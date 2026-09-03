"""Pure, immutable adapter for run-scoped corporate-action snapshots.

The adapter deliberately knows nothing about repositories or ORM objects.  A
provider (typically a :class:`DataChunkSession`) hands it already validated
``CashDividendEvent`` values; the resulting snapshot is the only object the
runtime needs to consume.  Hash material is canonical and excludes volatile
run metadata such as ``run_id`` and timestamps.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Iterable, Mapping
from uuid import UUID

from app.backtesting.data.errors import ProviderContractViolationError
from app.backtesting.dividends import CashDividendEvent

__all__ = ["RunCorporateActionEventSnapshot", "CorporateActionSnapshotConflict"]


class CorporateActionSnapshotConflict(ValueError):
    """Raised when the same event id is supplied with different content."""


def _json_value(value: Any) -> Any:
    if isinstance(value, (UUID, date, datetime)):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "value") and not isinstance(value, (str, bytes)):
        return _json_value(value.value)
    if isinstance(value, Mapping):
        return {str(k): _json_value(v) for k, v in value.items()}
    if is_dataclass(value):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(v) for v in value]
    return value


@dataclass(frozen=True, slots=True)
class RunCorporateActionEventSnapshot:
    """Frozen run-level company-action facts and their audit references."""

    cash_dividend_events: tuple[CashDividendEvent, ...]
    source_revisions: tuple[Mapping[str, Any], ...] = ()
    cash_date_rule_refs: tuple[str, ...] = ()
    timing_rule_refs: tuple[str, ...] = ()
    coverage_summary: Mapping[str, Any] = MappingProxyType({})
    snapshot_hash: str = ""

    def __post_init__(self) -> None:
        events = tuple(self.cash_dividend_events)
        if any(not isinstance(e, CashDividendEvent) for e in events):
            raise TypeError("cash_dividend_events must contain CashDividendEvent values")
        by_id: dict[UUID, CashDividendEvent] = {}
        for event in events:
            previous = by_id.get(event.event_id)
            if previous is not None and previous != event:
                raise CorporateActionSnapshotConflict(
                    f"event {event.event_id} appears with conflicting content"
                )
            by_id[event.event_id] = event
        ordered = tuple(sorted(by_id.values(), key=lambda e: (e.cash_effective_session_id, str(e.instrument_id), str(e.event_id))))
        object.__setattr__(self, "cash_dividend_events", ordered)
        object.__setattr__(self, "source_revisions", tuple(MappingProxyType(dict(x)) for x in self.source_revisions))
        object.__setattr__(self, "cash_date_rule_refs", tuple(str(x) for x in self.cash_date_rule_refs))
        object.__setattr__(self, "timing_rule_refs", tuple(str(x) for x in self.timing_rule_refs))
        object.__setattr__(self, "coverage_summary", MappingProxyType(dict(self.coverage_summary)))
        expected = self._compute_hash()
        if self.snapshot_hash and self.snapshot_hash != expected:
            raise ValueError("snapshot_hash does not match snapshot contents")
        object.__setattr__(self, "snapshot_hash", expected)

    def _hash_payload(self) -> Mapping[str, Any]:
        return {
            "cash_dividend_events": [_json_value(e) for e in self.cash_dividend_events],
            "source_revisions": [_json_value(x) for x in self.source_revisions],
            "cash_date_rule_refs": self.cash_date_rule_refs,
            "timing_rule_refs": self.timing_rule_refs,
            "coverage_summary": _json_value(self.coverage_summary),
        }

    def _compute_hash(self) -> str:
        payload = json.dumps(self._hash_payload(), sort_keys=True, separators=(",", ":"), default=str).encode()
        return hashlib.sha256(payload).hexdigest()

    @classmethod
    def from_events(cls, events: Iterable[CashDividendEvent], **metadata: Any) -> "RunCorporateActionEventSnapshot":
        """Construct a snapshot from provider-returned immutable events."""
        return cls(tuple(events), **metadata)

    @classmethod
    def from_chunk_sessions(
        cls,
        sessions: Iterable[Any],
        query_factory: Any,
        **metadata: Any,
    ) -> "RunCorporateActionEventSnapshot":
        """Read each frozen chunk once and combine its event facts.

        ``query_factory`` receives a chunk session and must return the
        already-bounded ``CorporateActionQuery`` for that chunk.  Keeping
        query construction outside this adapter prevents it from widening
        date boundaries or reaching a repository directly.
        """
        events: list[CashDividendEvent] = []
        revisions: list[Mapping[str, Any]] = []
        cash_rules: list[str] = []
        timing_rules: list[str] = []
        seen_metadata_ids: set[UUID] = set()
        for session in sessions:
            rows = session.corporate_actions(query_factory(session))
            for row in rows:
                event = (
                    row
                    if isinstance(row, CashDividendEvent)
                    else cls._event_from_provider_fact(row)
                )
                events.append(event)
                if event.event_id in seen_metadata_ids:
                    continue
                seen_metadata_ids.add(event.event_id)
                revisions.append(dict(event.source_evidence))
                cash_rules.append(str(event.source_evidence.get("cash_date_rule", "")))
                timing_rules.append(str(event.source_evidence.get("timing_rule", "")))
        metadata.setdefault("source_revisions", tuple(revisions))
        metadata.setdefault("cash_date_rule_refs", tuple(x for x in cash_rules if x))
        metadata.setdefault("timing_rule_refs", tuple(x for x in timing_rules if x))
        return cls.from_events(events, **metadata)

    @classmethod
    def from_data_provider(
        cls, sessions: Iterable[Any], query_factory: Any, **metadata: Any
    ) -> "RunCorporateActionEventSnapshot":
        """Build a run snapshot exclusively through DataChunkSession reads."""
        return cls.from_chunk_sessions(sessions, query_factory, **metadata)

    @staticmethod
    def _event_from_provider_fact(row: Any) -> CashDividendEvent:
        """Normalize one provider fact without allowing silent omission.

        The first runtime version accounts only for cash dividends.  Quantity
        actions and malformed cash facts are hard provider-contract failures;
        returning ``None`` here would make them disappear from the audit trail.
        """
        action_type = getattr(row, "action_type", None)
        attrs = getattr(row, "attributes", None)
        event_id = str(
            attrs.get("event_id", getattr(row, "event_id", ""))
            if isinstance(attrs, Mapping)
            else getattr(row, "event_id", "")
        )
        if action_type != "cash_dividend":
            quantity_action = action_type in {
                "split",
                "consolidation",
                "share_change",
            }
            raise ProviderContractViolationError(
                (
                    "quantity-class corporate actions are unsupported in formal v1"
                    if quantity_action
                    else "provider returned an unsupported corporate action type"
                ),
                details={
                    "event_id": event_id,
                    "action_type": action_type,
                    "reason_code": (
                        "quantity_corporate_action_unsupported"
                        if quantity_action
                        else "corporate_action_type_unsupported"
                    ),
                },
            )
        if not isinstance(attrs, Mapping):
            raise ProviderContractViolationError(
                "cash corporate action attributes are missing",
                details={"event_id": event_id, "reason_code": "corporate_action_attributes_missing"},
            )
        evidence = {
            str(key): str(value)
            for key, value in attrs.items()
            if value is not None
        }
        source = getattr(getattr(row, "evidence", None), "source", None)
        if source:
            evidence["source"] = str(source)
        required_evidence = ("calendar_id", "timezone", "cash_date_rule", "timing_rule")
        missing_evidence = tuple(
            name for name in required_evidence
            if not evidence.get(name)
        )
        if missing_evidence:
            raise ProviderContractViolationError(
                "cash corporate action evidence is incomplete",
                details={
                    "event_id": event_id,
                    "missing_evidence": missing_evidence,
                    "reason_code": "corporate_action_evidence_incomplete",
                },
            )
        source_payment = attrs.get("source_payment_date")
        source_arrival = attrs.get("source_arrival_date")
        if source_payment is None and source_arrival is None:
            raise ProviderContractViolationError(
                "cash corporate action has no source payment or arrival date",
                details={
                    "event_id": event_id,
                    "reason_code": "corporate_action_cash_date_unresolved",
                },
            )
        try:
            event_id_value = UUID(event_id)
            ex_date = row.ex_date
            record_date = date.fromisoformat(str(attrs["record_date"]))
            payment = date.fromisoformat(str(source_payment or source_arrival))
            arrival = date.fromisoformat(str(source_arrival or source_payment))
            effective = date.fromisoformat(str(attrs["cash_effective_date"]))
            amount = attrs["cash_amount_per_unit"]
            as_of = getattr(getattr(row, "evidence", None), "observed_at", None)
            if isinstance(as_of, datetime):
                as_of = as_of.date()
            if not isinstance(as_of, date):
                as_of = ex_date
            return CashDividendEvent(
                event_id=event_id_value,
                instrument_id=row.instrument_id,
                ex_date=ex_date,
                record_date=record_date,
                source_payment_date=payment,
                source_arrival_date=arrival,
                cash_effective_session_id=effective,
                amount_per_share=amount,
                source_evidence=evidence,
                as_of=as_of,
                currency=attrs.get("currency") or "CNY",
                cash_effective_phase=attrs.get("cash_effective_phase", "after_open_match"),
                derivation_rule_key=attrs.get("entitlement_rule", "record_date_entitlement"),
                derivation_rule_version=1,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderContractViolationError(
                "cash corporate action fact is malformed",
                details={
                    "event_id": event_id,
                    "reason_code": "corporate_action_fact_invalid",
                    "error_type": type(exc).__name__,
                },
            ) from exc

    def merge(self, other: "RunCorporateActionEventSnapshot") -> "RunCorporateActionEventSnapshot":
        """Merge chunk snapshots, rejecting conflicting duplicate events."""
        if not isinstance(other, RunCorporateActionEventSnapshot):
            raise TypeError("other must be RunCorporateActionEventSnapshot")
        return RunCorporateActionEventSnapshot.from_events(
            (*self.cash_dividend_events, *other.cash_dividend_events),
            source_revisions=(*self.source_revisions, *other.source_revisions),
            cash_date_rule_refs=tuple(dict.fromkeys((*self.cash_date_rule_refs, *other.cash_date_rule_refs))),
            timing_rule_refs=tuple(dict.fromkeys((*self.timing_rule_refs, *other.timing_rule_refs))),
            coverage_summary={**self.coverage_summary, **other.coverage_summary},
        )
