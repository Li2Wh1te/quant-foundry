"""Immutable run-level rule snapshot domain objects.

A run rule snapshot has two layers:

- one *run-level* row recording the selected rule package, parser
  revision, named-exception set, data cutoff, and the total snapshot hash;
- per-instrument *segment* rows recording the exact ordinary and
  exception fact references, normalized values, capability declarations,
  and provenance actually used inside every validity segment of the run.

Backtest execution must read only these frozen snapshots; it never
re-parses the live fact tables.  The ``snapshot_hash`` is computed over
canonical content only: ``run_id`` (assigned later by persistence),
object reprs, collection order, and query time cannot influence it.
This module is free of ORM, database session, FastAPI, Tushare, and any
concrete data-source client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Mapping
from uuid import UUID

from app.backtesting.domain import DomainValidationError, _aware_datetime
from app.instruments.domain import VersionedReference, _required_label
from app.instruments.rules.contracts import (
    canonical_payload,
    deep_freeze,
    reference_display,
    stable_hash,
)


def _validate_date(value: date, field_name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise DomainValidationError(f"{field_name} must be a calendar date")
    return value


def _validate_optional_interval(
    effective_from: date, effective_to: date | None, *, prefix: str
) -> None:
    _validate_date(effective_from, f"{prefix}effective_from")
    if effective_to is None:
        return
    _validate_date(effective_to, f"{prefix}effective_to")
    if effective_to <= effective_from:
        raise DomainValidationError(
            f"{prefix}effective_to must be later than effective_from "
            "(half-open interval)"
        )


@dataclass(frozen=True, slots=True)
class FactProvenance:
    """Full source evidence of one contributing persisted fact row.

    Every field an auditor needs to re-fetch and re-judge the exact fact
    version is preserved: identity, validity window, knowledge time,
    quality flag, and the fixture marker.
    """

    fact_reference: VersionedReference
    source: str
    source_revision: str | None
    valid_from: date | None
    valid_to: date | None
    known_at: datetime
    quality_status: str
    fixture_only: bool
    # These fields are optional for backwards-compatible fixture construction;
    # formal preflight always supplies both values from the persisted fact.
    observed_at: datetime | None = None
    content_hash: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.fact_reference, VersionedReference):
            raise DomainValidationError(
                "fact_reference must be a VersionedReference"
            )
        object.__setattr__(self, "source", _required_label(self.source, "source"))
        if self.valid_from is not None:
            _validate_date(self.valid_from, "valid_from")
        if self.valid_to is not None:
            _validate_date(self.valid_to, "valid_to")
        object.__setattr__(
            self,
            "known_at",
            _aware_datetime(self.known_at, "known_at").astimezone(UTC),
        )
        if self.observed_at is not None:
            object.__setattr__(
                self,
                "observed_at",
                _aware_datetime(self.observed_at, "observed_at").astimezone(UTC),
            )
        if not isinstance(self.quality_status, str) or not self.quality_status:
            raise DomainValidationError("quality_status must be a non-empty string")
        if not isinstance(self.fixture_only, bool):
            raise DomainValidationError("fixture_only must be a boolean")
        if self.content_hash is not None:
            if not isinstance(self.content_hash, str) or not self.content_hash.strip():
                raise DomainValidationError(
                    "content_hash must be non-blank text when provided"
                )
            normalized_hash = self.content_hash.strip()
            if len(normalized_hash) != 64 or any(
                character not in "0123456789abcdef" for character in normalized_hash
            ):
                raise DomainValidationError(
                    "content_hash must be a lowercase SHA-256 hex digest"
                )
            object.__setattr__(self, "content_hash", normalized_hash)

    def to_payload(self) -> dict[str, Any]:
        """Canonical JSON-ready representation used in hashes and JSONB."""

        return {
            "fact_key": self.fact_reference.key,
            "fact_version": self.fact_reference.version,
            "source": self.source,
            "source_revision": self.source_revision,
            "valid_from": (
                None if self.valid_from is None else self.valid_from.isoformat()
            ),
            "valid_to": (
                None if self.valid_to is None else self.valid_to.isoformat()
            ),
            "known_at": self.known_at.isoformat(),
            "observed_at": (
                None if self.observed_at is None else self.observed_at.isoformat()
            ),
            "quality_status": self.quality_status,
            "fixture_only": self.fixture_only,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class InstrumentRuleSnapshotSegment:
    """The frozen rules actually used for one instrument over one interval.

    ``normalized_values`` and ``capability_declarations`` are deep-frozen
    so the hashed content can never be edited afterwards.  Segments are
    half-open ``[effective_from, effective_to)``; ``effective_to=None``
    means the segment extends to the end of the backtest window.
    """

    instrument_id: UUID
    effective_from: date
    effective_to: date | None
    normal_fact_reference: VersionedReference
    exception_fact_reference: VersionedReference | None
    normalized_values: Mapping[str, Any]
    capability_declarations: Mapping[str, str]
    provenance: Mapping[str, Any]
    resolution_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, UUID):
            raise DomainValidationError(
                "instrument_id must be a UUID (stable instrument identity)"
            )
        _validate_optional_interval(
            self.effective_from, self.effective_to, prefix=""
        )
        if not isinstance(self.normal_fact_reference, VersionedReference):
            raise DomainValidationError(
                "normal_fact_reference must be a VersionedReference"
            )
        if self.exception_fact_reference is not None and not isinstance(
            self.exception_fact_reference, VersionedReference
        ):
            raise DomainValidationError(
                "exception_fact_reference must be a VersionedReference "
                "when provided"
            )
        if not isinstance(self.normalized_values, Mapping):
            raise DomainValidationError("normalized_values must be a mapping")
        if not isinstance(self.capability_declarations, Mapping):
            raise DomainValidationError(
                "capability_declarations must be a mapping"
            )
        if not isinstance(self.provenance, Mapping):
            raise DomainValidationError("provenance must be a mapping")
        object.__setattr__(
            self, "resolution_hash", _required_label(
                self.resolution_hash, "resolution_hash"
            )
        )
        object.__setattr__(
            self, "normalized_values", deep_freeze(dict(self.normalized_values))
        )
        object.__setattr__(
            self,
            "capability_declarations",
            deep_freeze(dict(self.capability_declarations)),
        )
        object.__setattr__(
            self, "provenance", deep_freeze(dict(self.provenance))
        )

    def sort_key(self) -> tuple[str, str]:
        """Stable ordering key independent of insertion order."""

        return (
            str(self.instrument_id),
            self.effective_from.isoformat(),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "instrument_id": str(self.instrument_id),
            "effective_from": self.effective_from.isoformat(),
            "effective_to": (
                None
                if self.effective_to is None
                else self.effective_to.isoformat()
            ),
            "normal_fact_reference": canonical_payload(
                self.normal_fact_reference
            ),
            "exception_fact_reference": (
                None
                if self.exception_fact_reference is None
                else canonical_payload(self.exception_fact_reference)
            ),
            "normalized_values": canonical_payload(self.normalized_values),
            "capability_declarations": canonical_payload(
                self.capability_declarations
            ),
            "provenance": canonical_payload(self.provenance),
            "resolution_hash": self.resolution_hash,
        }


@dataclass(frozen=True, slots=True)
class RunRuleSnapshotBundle:
    """The complete immutable rule snapshot of one formal run.

    ``run_id`` is ``None`` until the bundle is bound to a created run;
    it never participates in ``snapshot_hash``.  Segments are stored
    deduplicated and stably ordered so input order cannot change the
    hash.
    """

    rule_package_reference: VersionedReference
    rule_package_semantic_hash: str
    parser_revision: str
    exception_set_reference: VersionedReference | None
    exception_set_hash: str | None
    data_cutoff: datetime
    instrument_segments: tuple[InstrumentRuleSnapshotSegment, ...]
    run_id: UUID | None = None
    snapshot_hash: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.rule_package_reference, VersionedReference):
            raise DomainValidationError(
                "rule_package_reference must be a VersionedReference"
            )
        object.__setattr__(
            self,
            "rule_package_semantic_hash",
            _required_label(
                self.rule_package_semantic_hash, "rule_package_semantic_hash"
            ),
        )
        object.__setattr__(
            self, "parser_revision", _required_label(
                self.parser_revision, "parser_revision"
            )
        )
        if self.exception_set_reference is not None and not isinstance(
            self.exception_set_reference, VersionedReference
        ):
            raise DomainValidationError(
                "exception_set_reference must be a VersionedReference when "
                "provided"
            )
        if self.exception_set_reference is None:
            if self.exception_set_hash is not None:
                raise DomainValidationError(
                    "exception_set_hash must be None when no exception set "
                    "is selected"
                )
        elif self.exception_set_hash is None or not isinstance(
            self.exception_set_hash, str
        ):
            raise DomainValidationError(
                "exception_set_hash must record the selected set's content "
                "hash"
            )
        object.__setattr__(
            self,
            "data_cutoff",
            _aware_datetime(self.data_cutoff, "data_cutoff").astimezone(UTC),
        )
        if self.run_id is not None and not isinstance(self.run_id, UUID):
            raise DomainValidationError("run_id must be a UUID when provided")

        segments: dict[tuple[str, str], InstrumentRuleSnapshotSegment] = {}
        for segment in self.instrument_segments:
            if not isinstance(segment, InstrumentRuleSnapshotSegment):
                raise DomainValidationError(
                    "instrument_segments must contain "
                    "InstrumentRuleSnapshotSegment instances"
                )
            key = segment.sort_key()
            if key in segments:
                raise DomainValidationError(
                    "duplicate instrument rule snapshot segment for "
                    f"instrument {key[0]} starting {key[1]}"
                )
            segments[key] = segment
        ordered = (segments[key] for key in sorted(segments))
        object.__setattr__(
            self, "instrument_segments", tuple(ordered)
        )
        object.__setattr__(self, "snapshot_hash", self._compute_snapshot_hash())

    def segment_for(
        self, instrument_id: UUID, effective_at: date | datetime
    ) -> InstrumentRuleSnapshotSegment:
        """Return the single frozen segment covering ``effective_at``.

        Runtime callers use this method instead of inspecting segment rows or
        consulting live rule facts.  Missing coverage is a hard failure.
        """

        if not isinstance(instrument_id, UUID):
            raise DomainValidationError("instrument_id must be a UUID")
        if isinstance(effective_at, datetime):
            effective_date = _aware_datetime(
                effective_at, "effective_at"
            ).date()
        elif isinstance(effective_at, date):
            effective_date = effective_at
        else:
            raise DomainValidationError("effective_at must be a date or datetime")
        matches = [
            segment
            for segment in self.instrument_segments
            if segment.instrument_id == instrument_id
            and segment.effective_from <= effective_date
            and (
                segment.effective_to is None
                or effective_date < segment.effective_to
            )
        ]
        if not matches:
            raise DomainValidationError(
                "frozen rule snapshot has no unique segment for "
                f"instrument {instrument_id} on {effective_date.isoformat()}"
            )
        # Legacy bundles may use an open-ended first segment as a sentinel;
        # when such a bundle overlaps a later explicit segment, the segment
        # with the latest start is the deterministic effective choice.
        return max(matches, key=lambda segment: segment.effective_from)

    def for_run(self, run_id: UUID) -> "RunRuleSnapshotBundle":
        """Bind an unbound bundle to a run without changing its hash."""

        if not isinstance(run_id, UUID):
            raise DomainValidationError("run_id must be a UUID")
        if self.run_id is not None and self.run_id != run_id:
            raise DomainValidationError("snapshot bundle is already bound to another run")
        return RunRuleSnapshotBundle(
            rule_package_reference=self.rule_package_reference,
            rule_package_semantic_hash=self.rule_package_semantic_hash,
            parser_revision=self.parser_revision,
            exception_set_reference=self.exception_set_reference,
            exception_set_hash=self.exception_set_hash,
            data_cutoff=self.data_cutoff,
            instrument_segments=self.instrument_segments,
            run_id=run_id,
        )

    def verify_hash(self) -> str:
        """Recompute and verify the immutable snapshot hash."""

        expected = self._compute_snapshot_hash()
        if expected != self.snapshot_hash:
            raise DomainValidationError(
                "rule snapshot content does not match its snapshot_hash"
            )
        return expected

    def _compute_snapshot_hash(self) -> str:
        payload = {
            "kind": "run_rule_snapshot",
            # run_id is deliberately excluded: it is assigned after the
            # preflight produced this bundle and must not change identity.
            "rule_package_reference": canonical_payload(
                self.rule_package_reference
            ),
            "rule_package_semantic_hash": self.rule_package_semantic_hash,
            "parser_revision": self.parser_revision,
            "exception_set_reference": (
                None
                if self.exception_set_reference is None
                else canonical_payload(self.exception_set_reference)
            ),
            "exception_set_hash": self.exception_set_hash,
            "data_cutoff": self.data_cutoff.isoformat(),
            "instrument_segments": [
                segment.to_payload() for segment in self.instrument_segments
            ],
        }
        return stable_hash(payload)

    def display_summary(self) -> str:
        """Concise operator-facing description of the frozen selection."""

        parts = [
            reference_display(self.rule_package_reference) or "",
            f"parser={self.parser_revision}",
        ]
        if self.exception_set_reference is not None:
            parts.append(
                f"exceptions={reference_display(self.exception_set_reference)}"
            )
        parts.append(f"segments={len(self.instrument_segments)}")
        return "; ".join(part for part in parts if part)


__all__ = [
    "FactProvenance",
    "InstrumentRuleSnapshotSegment",
    "RunRuleSnapshotBundle",
]
