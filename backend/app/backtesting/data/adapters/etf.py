"""Read-only ETF data adapter for the generic backtesting data contract.

The adapter projects the existing ETF ingestion tables (``etf_daily_bars``,
``etf_adjustment_factors``, ``instrument_code_mappings``,
``trading_calendar_days``) onto the generic fact objects
(:class:`Bar`, :class:`AdjustedSeriesPoint`, evidenced code mappings) and
serves them through the production PIT read path.

Hard boundaries implemented here:

* the raw tables stay the single source of truth: nothing is rewritten,
  repaired, or back-filled; zero/negative prices and illegal OHLC rows are
  projected as ``invalid`` facts and keep their raw values;
* history is reachable only through stable ``instrument_id`` plus
  evidenced PIT mappings; the current ``EtfCode.etf_id`` association is
  never used to stitch cross-code history;
* daily bars carry no reliable source ``known_at``, so they are declared
  ``non_strict`` PIT and their ``updated_at`` is used only as an
  observation/revision marker, never as knowledge-time evidence;
* adjustment factors follow the approved first-version contract
  ``tushare_adj_factor_native@1``: positive factors only, selected by
  ``effective_date <= data_cutoff``, served only when the policy has been
  verified and activated, never with fabricated revisions;
* this module imports no network client of any kind: backtest reads can
  never trigger a Tushare call.

Integration boundary (task 03-08C): the adapter, its preflight summary,
and :func:`build_data_preflight_payloads` feed the existing
``backtest_data_preflight`` record and ``/data-preflight`` API.  The
persistence chain is exercised end to end at the repository level
(see ``tests/test_etf_data_adapter.py::PersistenceChainTestCase``);
automatic invocation inside a live backtest run lands together with the
formal provider registration, which this task package deliberately
excludes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Mapping, Protocol, Sequence
from uuid import UUID

from app.backtesting.calendar_axis import SessionPoint, SessionWindow
from app.backtesting.data.errors import (
    HistoryIncompleteError,
    IdentityMappingConflictError,
    IdentityMappingIncompleteError,
    InvalidDataRequestError,
    ProviderContractViolationError,
    UnsupportedCapabilityError,
)
from app.backtesting.data.facts import AdjustedSeriesPoint, Bar, FactEvidence
from app.backtesting.data.pit_history import (
    PITMappingResolution,
    SegmentedAdjustedSeries,
    SegmentedBarHistory,
    resolve_pit_mappings,
    read_segmented_adjusted_series,
    read_segmented_history,
)
from app.backtesting.data.reports import canonical_hash
from app.backtesting.data.requests import (
    DateRange,
    LookbackWindow,
    PriceBasis,
    QualityStatus,
    QueryBoundary,
)
from app.backtesting.domain import _aware_datetime
from app.instruments.domain import (
    CorporateActionRequirement,
    InstrumentCapabilities,
    InstrumentCodeMapping,
    InstrumentDisplay,
    InstrumentSpec,
    MappingConflictError,
    MappingCoverageGapError,
)
from app.instruments.references import VersionedReference

__all__ = [
    "ADJUSTMENT_SERIES_POLICY",
    "ETF_CALENDAR_ID",
    "ETF_PROVIDER_KEY",
    "ETF_RULE_PACKAGE",
    "EtfFactsAdapter",
    "build_data_preflight_payloads",
]


ETF_PROVIDER_KEY = "etf_ingestion"
"""Stable provider key declared by the ETF ingestion data foundation."""

ETF_RULE_PACKAGE = ("china_listed_etf_rules", 1)
"""Versioned rule package whose frozen defaults back the spec projection."""

ADJUSTMENT_SERIES_POLICY = ("tushare_adj_factor_native", 1)
"""First-version adjustment-series policy (key, version)."""

ETF_CALENDAR_ID = "china_sse"
"""Calendar id projected from the SSE trading-calendar facts."""

_ETF_SESSION_TEMPLATE = VersionedReference(key="china_etf_full_day", version=1)
"""Session template referenced by every projected ETF instrument spec."""

# Frozen first-version trading parameters for China-listed ETFs under
# china_listed_etf_rules@1.  They are contract constants of this adapter
# version, not per-row data: the source tables carry no tick/lot columns.
_ETF_SPEC_DEFAULTS: dict[str, object] = {
    "price_precision": 3,
    "quantity_precision": 0,
    "price_tick": Decimal("0.001"),
    "lot_size": Decimal("100"),
    "minimum_order_quantity": Decimal("100"),
    "contract_multiplier": Decimal("1"),
}
_ETF_CAPABILITIES = InstrumentCapabilities(
    position_sides=frozenset({"long"}),
    order_types=frozenset({"limit"}),
    margin_supported=False,
    corporate_action_requirement=CorporateActionRequirement.NOT_APPLICABLE,
)

_SSE_DAY_SESSIONS = (SessionWindow(time(9, 30), time(11, 30), label="morning"),
                     SessionWindow(time(13, 0), time(15, 0), label="afternoon"))


# ---------------------------------------------------------------------------
# Read-only ports (thin wrappers over the ingestion query repositories)
# ---------------------------------------------------------------------------


class CodeMappingsPort(Protocol):
    """Resolves evidenced PIT mappings for one instrument/source pair."""

    def __call__(
        self,
        instrument_id: UUID,
        *,
        source: str,
        start_date: date,
        end_date: date,
        data_cutoff: datetime,
    ) -> Sequence[InstrumentCodeMapping]:
        ...


class DailyBarsPort(Protocol):
    """Reads stored daily bars keyed by one source code."""

    def __call__(
        self, ts_code: str, start_date: date | None, end_date: date | None
    ) -> Sequence[object]:
        ...


class AdjustmentFactorsPort(Protocol):
    """Reads stored adjustment factors keyed by one source code."""

    def __call__(
        self, ts_code: str, start_date: date | None, end_date: date | None
    ) -> Sequence[object]:
        ...


class TradingDaysPort(Protocol):
    """Reads open trading days for one exchange inside a window."""

    def __call__(self, exchange: str, start_date: date, end_date: date) -> list[date]:
        ...


# ---------------------------------------------------------------------------
# Source row shapes (structural; real ORM rows satisfy these)
# ---------------------------------------------------------------------------


class DailyBarRow(Protocol):
    """Structural view of one ``etf_daily_bars`` row."""

    ts_code: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    vol: Decimal
    amount: Decimal
    updated_at: datetime


class AdjustmentFactorRow(Protocol):
    """Structural view of one ``etf_adjustment_factors`` row."""

    ts_code: str
    trade_date: date
    adj_factor: Decimal
    updated_at: datetime


def _bar_quality(row: DailyBarRow) -> QualityStatus:
    """Classify a raw bar row without repairing anything.

    A row is consumable (``complete``) only when every price is strictly
    positive and ``low <= high``; volume and amount must be non-negative.
    Everything else stays an explicit ``invalid`` fact with its original
    values so coverage and preflight can block on it.
    """

    prices = (row.open, row.high, row.low, row.close)
    if any(price is None or price <= 0 for price in prices):
        return QualityStatus.INVALID
    if row.low > row.high:
        return QualityStatus.INVALID
    if (row.vol is not None and row.vol < 0) or (
        row.amount is not None and row.amount < 0
    ):
        return QualityStatus.INVALID
    return QualityStatus.COMPLETE


@dataclass(frozen=True, slots=True)
class EtfFactsAdapter:
    """Read-only projection of stored ETF facts onto the data contract.

    Every dependency is an injected read-only callable, so the adapter can
    never reach past the fact tables into a live external source.  The
    adapter is immutable: ``adjustment_active`` and its verification
    evidence are fixed at construction and cannot be flipped at runtime.
    """

    code_mappings: CodeMappingsPort
    daily_bars: DailyBarsPort
    adjustment_factors: AdjustmentFactorsPort
    trading_days: TradingDaysPort
    source: str = "tushare"
    calendar_exchange: str = "SSE"
    clock: datetime | None = None
    adjustment_active: bool = False
    adjustment_verification_evidence: str | None = None

    def __post_init__(self) -> None:
        if self.clock is not None:
            _aware_datetime(self.clock, "clock")
        if self.adjustment_active and not (
            isinstance(self.adjustment_verification_evidence, str)
            and self.adjustment_verification_evidence.strip()
        ):
            raise InvalidDataRequestError(
                "an active tushare_adj_factor_native@1 policy requires "
                "real-source verification evidence"
            )

    # ------------------------------------------------------------------
    # Identity mappings
    # ------------------------------------------------------------------

    def resolve_mappings(
        self,
        instrument_id: UUID,
        *,
        start_date: date,
        end_date: date,
        data_cutoff: datetime,
    ) -> tuple[InstrumentCodeMapping, ...]:
        """Resolve visible mappings, converting DB errors to stable codes.

        The instruments repository raises domain-layer
        ``MappingCoverageGapError`` / ``MappingConflictError``; crossing
        the provider boundary those become the stable data-contract codes
        ``identity_mapping_incomplete`` / ``identity_mapping_conflict``
        so callers never parse exception text.
        """

        try:
            return tuple(
                self.code_mappings(
                    instrument_id,
                    source=self.source,
                    start_date=start_date,
                    end_date=end_date,
                    data_cutoff=data_cutoff,
                )
            )
        except MappingCoverageGapError as exc:
            raise IdentityMappingIncompleteError(
                "no complete PIT code mapping covers the requested window",
                details={
                    "instrument_id": str(instrument_id),
                    "source": self.source,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "reason": str(exc),
                },
            ) from exc
        except MappingConflictError as exc:
            raise IdentityMappingConflictError(
                "PIT code mappings overlap inside the requested window",
                details={
                    "instrument_id": str(instrument_id),
                    "source": self.source,
                    "reason": str(exc),
                },
            ) from exc

    # ------------------------------------------------------------------
    # Instrument display and spec projection
    # ------------------------------------------------------------------

    def project_display(self, row: object) -> InstrumentDisplay | None:
        """Project one ``etf_codes`` row onto the generic display object.

        Display fields stay ``None`` when the source does not provide
        them; only the stable identity is mandatory.  A row without its
        entity binding (``etf_id``) has no stable identity to project and
        yields ``None`` instead of a fabricated one.
        """

        entity_id = getattr(row, "etf_id", None)
        if not isinstance(entity_id, UUID):
            return None
        ts_code = getattr(row, "ts_code", None)
        trading_code = (
            ts_code.split(".", 1)[0]
            if isinstance(ts_code, str) and "." in ts_code
            else ts_code
        )
        return InstrumentDisplay(
            instrument_id=entity_id,
            trading_code=trading_code if isinstance(trading_code, str) else None,
            name=getattr(row, "cname", None),
            display_name=getattr(row, "csname", None),
        )

    def project_instrument_spec(self, row: object) -> InstrumentSpec | None:
        """Project one ``etf_codes`` row onto a complete engine spec.

        Trading-critical fields missing from the source table are frozen
        defaults of :data:`ETF_RULE_PACKAGE` (``china_listed_etf_rules@1``).
        Rows without the mandatory identity/exchange/listing facts yield
        ``None`` — the provider contract forbids degrading into a spec
        full of placeholders.  The validity window starts strictly at
        ``list_date``: falling back to ``setup_date`` would admit funds
        that exist but are not listed yet onto a tradable timeline, so a
        missing ``list_date`` makes the spec unresolvable.
        """

        display = self.project_display(row)
        if display is None:
            return None
        exchange = getattr(row, "exchange", None)
        list_date = getattr(row, "list_date", None)
        if not isinstance(exchange, str) or not exchange.strip():
            return None
        if not isinstance(list_date, date) or isinstance(list_date, datetime):
            return None
        valid_from = datetime(list_date.year, list_date.month, list_date.day, tzinfo=UTC)
        return InstrumentSpec(
            instrument_id=display.instrument_id,
            display=display,
            asset_class="etf",
            exchange=exchange.upper(),
            currency="CNY",
            calendar_id=ETF_CALENDAR_ID,
            price_precision=_ETF_SPEC_DEFAULTS["price_precision"],
            quantity_precision=_ETF_SPEC_DEFAULTS["quantity_precision"],
            price_tick=_ETF_SPEC_DEFAULTS["price_tick"],
            lot_size=_ETF_SPEC_DEFAULTS["lot_size"],
            minimum_order_quantity=_ETF_SPEC_DEFAULTS["minimum_order_quantity"],
            contract_multiplier=_ETF_SPEC_DEFAULTS["contract_multiplier"],
            trading_session_template=_ETF_SESSION_TEMPLATE,
            valid_from=valid_from,
            valid_to=None,
            capabilities=_ETF_CAPABILITIES,
        )

    # ------------------------------------------------------------------
    # Bar projection and segmented history
    # ------------------------------------------------------------------

    def project_bar(self, row: DailyBarRow, instrument_id: UUID) -> Bar:
        """Project one raw ``etf_daily_bars`` row onto a generic ``Bar``.

        ``price_basis`` is always ``raw``, frequency ``1d``.  ``known_at``
        stays ``None`` because the table has no reliable knowledge-time
        column; ``updated_at`` is carried as ``observed_at`` only.
        """

        quality = _bar_quality(row)
        return Bar(
            instrument_id=instrument_id,
            trade_date=row.trade_date,
            frequency="1d",
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.vol,
            amount=row.amount,
            price_basis=PriceBasis.RAW,
            evidence=FactEvidence(
                source=self.source,
                observed_at=row.updated_at,
                quality_status=quality,
                known_at=None,
            ),
        )

    @staticmethod
    def require_row_code(row: object, requested_source_code: str) -> str:
        """Verify a stored row belongs to the requested source code.

        The generic ``Bar``/``AdjustedSeriesPoint`` envelopes no longer
        carry the source code, so a repository bug that returns another
        code's rows would be invisible after projection and could poison
        one identity's history with another's facts.  The check runs
        before projection and blocks with a stable provider-contract code.
        """

        row_code = getattr(row, "ts_code", None)
        if not isinstance(row_code, str) or row_code != requested_source_code:
            raise ProviderContractViolationError(
                "the repository returned a row keyed by another source "
                "code than the requested PIT segment",
                details={
                    "requested_source_code": requested_source_code,
                    "returned_source_code": (
                        row_code if isinstance(row_code, str) else None
                    ),
                },
            )
        return row_code

    def _segment_bar_reader(self, instrument_id: UUID):
        """Build the per-segment reader over the stored daily-bar table."""

        adapter = self

        class _Reader:
            def read_bars(self, source_code: str, start_date: date, end_date: date):
                projected = []
                for row in adapter.daily_bars(source_code, start_date, end_date):
                    adapter.require_row_code(row, source_code)
                    if start_date <= row.trade_date <= end_date:
                        projected.append(adapter.project_bar(row, instrument_id))
                return projected

        return _Reader()

    def resolve(
        self,
        instrument_id: UUID,
        *,
        sessions: Sequence[date],
        data_cutoff: datetime,
    ) -> PITMappingResolution:
        """Bind every requested session to exactly one evidenced code."""

        if not sessions:
            raise HistoryIncompleteError(
                "the requested window contains no trading sessions"
            )
        mappings = self.resolve_mappings(
            instrument_id,
            start_date=min(sessions),
            end_date=max(sessions),
            data_cutoff=data_cutoff,
        )
        return resolve_pit_mappings(
            instrument_id,
            source=self.source,
            sessions=sessions,
            mappings=mappings,
            data_cutoff=data_cutoff,
        )

    def bars(
        self,
        instrument_id: UUID,
        *,
        resolution: PITMappingResolution,
    ) -> SegmentedBarHistory:
        """Read and stitch one stable-identity bar series by mapping.

        ETF daily bars keep no reliable source ``known_at``, so the read
        runs in non-strict fact mode: bars without knowledge-time evidence
        are served as latest authoritative revisions, while any bar whose
        ``known_at`` lands after ``data_cutoff`` still blocks.
        """

        return read_segmented_history(
            resolution,
            self._segment_bar_reader(instrument_id),
            allow_non_strict_facts=True,
        )

    # ------------------------------------------------------------------
    # Adjustment factors
    # ------------------------------------------------------------------

    def project_factor(self, row: AdjustmentFactorRow, instrument_id: UUID) -> AdjustedSeriesPoint:
        """Project one factor row; the effective date is ``trade_date``."""

        return AdjustedSeriesPoint(
            instrument_id=instrument_id,
            point_date=row.trade_date,
            price_basis=PriceBasis.QFQ,
            adj_factor=row.adj_factor,
            evidence=FactEvidence(
                source=self.source,
                observed_at=row.updated_at,
                quality_status=QualityStatus.COMPLETE,
                known_at=None,
            ),
        )

    def adjusted_series(
        self,
        instrument_id: UUID,
        *,
        resolution: PITMappingResolution,
        price_basis: PriceBasis,
    ) -> SegmentedAdjustedSeries:
        """Read factors per mapped segment under the activation gate."""

        if price_basis is PriceBasis.RAW:
            raise InvalidDataRequestError(
                "raw prices need no adjustment series"
            )
        if not self.adjustment_active:
            raise UnsupportedCapabilityError(
                "the tushare_adj_factor_native@1 policy is not verified "
                "and active; adjusted series are blocked",
                details={
                    "policy_key": ADJUSTMENT_SERIES_POLICY[0],
                    "policy_version": ADJUSTMENT_SERIES_POLICY[1],
                },
            )
        adapter = self

        class _FactorReader:
            def read_factors(self, source_code: str, start_date: date, end_date: date):
                points = []
                for row in adapter.adjustment_factors(source_code, start_date, end_date):
                    adapter.require_row_code(row, source_code)
                    if not (start_date <= row.trade_date <= end_date):
                        continue
                    point = adapter.project_factor(row, instrument_id)
                    if point.price_basis is not price_basis:
                        continue
                    points.append(point)
                return points

        return read_segmented_adjusted_series(resolution, _FactorReader())

    # ------------------------------------------------------------------
    # Calendar projection
    # ------------------------------------------------------------------

    def session_points(self, start_date: date, end_date: date) -> tuple[SessionPoint, ...]:
        """Project stored open trading days onto named calendar sessions."""

        days = sorted(set(self.trading_days(self.calendar_exchange, start_date, end_date)))
        return tuple(
            SessionPoint(
                session_date=day,
                session_id=f"{ETF_CALENDAR_ID}@{day.isoformat()}",
                timezone="Asia/Shanghai",
                sessions=_SSE_DAY_SESSIONS,
            )
            for day in days
        )

    # ------------------------------------------------------------------
    # Coverage, PIT status, and revision summaries
    # ------------------------------------------------------------------

    @staticmethod
    def coverage_summary(
        expected_sessions: Sequence[date],
        returned_dates: Sequence[date],
    ) -> dict[str, object]:
        """Structured coverage summary in the preflight-report shape.

        Duplicate and out-of-window returns are recorded explicitly and
        downgrade the status to ``partial``: deduplicating silently would
        let a buggy reader report complete coverage while serving the same
        session twice.
        """

        expected_set = set(expected_sessions)
        seen: set[date] = set()
        duplicates: list[date] = []
        out_of_window: list[date] = []
        for day in returned_dates:
            if day not in expected_set:
                out_of_window.append(day)
            elif day in seen:
                duplicates.append(day)
            else:
                seen.add(day)
        missing = sorted(
            day.isoformat() for day in expected_set if day not in seen
        )
        anomalies = bool(duplicates or out_of_window)
        if missing or anomalies:
            status = "partial"
        else:
            status = "complete" if len(seen) == len(expected_set) else "partial"
        return {
            "expected_sessions": len(set(expected_sessions)),
            "returned_sessions": len(seen),
            "missing_sessions": missing,
            "duplicate_sessions": sorted(day.isoformat() for day in duplicates),
            "out_of_window_sessions": sorted(
                day.isoformat() for day in out_of_window
            ),
            "status": status,
        }

    def pit_status(self) -> dict[str, object]:
        """Per-fact-family PIT declarations for the run metadata.

        ``adjustment_factors`` is the approved first-version policy marker,
        not a PIT support level: per the data protocol, factors follow the
        ``effective_date <= data_cutoff`` contract and never trigger the
        run-level ``non_strict_pit`` flag.
        """

        return {
            "instrument_code_mappings": "strict",
            "daily_bars": "non_strict",
            "adjustment_factors": f"{ADJUSTMENT_SERIES_POLICY[0]}"
            f"@{ADJUSTMENT_SERIES_POLICY[1]}:effective_date_cutoff",
            "trading_calendar": "non_strict",
        }

    def revision_stamp(
        self,
        rows: Sequence[DailyBarRow] | Sequence[AdjustmentFactorRow],
    ) -> str | None:
        """Latest observed timestamp across one family of source rows."""

        stamps = [row.updated_at for row in rows if row.updated_at is not None]
        return max(stamps).isoformat() if stamps else None

    def preflight_summary(
        self,
        *,
        instrument_ids: Sequence[UUID],
        expected_sessions: Sequence[date],
        bars_by_instrument: Mapping[UUID, Sequence[date]],
        factors_by_instrument: Mapping[UUID, Sequence[date]] | None = None,
        mappings_by_instrument: Mapping[UUID, Sequence[InstrumentCodeMapping]]
        | None = None,
        daily_rows: Sequence[DailyBarRow] = (),
        factor_rows: Sequence[AdjustmentFactorRow] = (),
        blocking_issues: Sequence[Mapping[str, object]] = (),
    ) -> dict[str, object]:
        """Assemble the machine summary consumed by result records.

        The content deliberately excludes generation time, database keys,
        and credentials so identical data facts hash identically; the hash
        changes when source revisions, coverage, mappings, or the
        adjustment-policy activation state change.
        """

        factor_coverage: dict[str, object] = {}
        if factors_by_instrument is not None:
            factor_coverage = {
                str(instrument_id): self.coverage_summary(
                    expected_sessions, factors_by_instrument.get(instrument_id, ())
                )
                for instrument_id in instrument_ids
            }
        mapping_summary: dict[str, object] = {}
        if mappings_by_instrument is not None:
            mapping_summary = {
                str(instrument_id): [
                    {
                        "source_code": mapping.source_code,
                        "trading_code": mapping.trading_code,
                        "valid_from": mapping.valid_from.isoformat(),
                        "valid_to": (
                            mapping.valid_to.isoformat()
                            if mapping.valid_to is not None
                            else None
                        ),
                        "mapping_source": mapping.mapping_source,
                        "evidence": mapping.evidence,
                        "known_at": mapping.known_at.isoformat(),
                        "source_revision": mapping.source_revision,
                    }
                    for mapping in mappings_by_instrument.get(instrument_id, ())
                ]
                for instrument_id in instrument_ids
            }
        summary: dict[str, object] = {
            "provider_key": ETF_PROVIDER_KEY,
            "data_contract_version": 1,
            "adjustment_series_policy": {
                "key": ADJUSTMENT_SERIES_POLICY[0],
                "version": ADJUSTMENT_SERIES_POLICY[1],
                "active": self.adjustment_active,
                "factor_cutoff_rule": "effective_date <= data_cutoff",
            },
            # Audit evidence for why the adjustment policy may be active:
            # absent evidence with an active policy is a construction error.
            "adjustment_series_validation": {
                "active": self.adjustment_active,
                "verification_evidence": (
                    self.adjustment_verification_evidence
                    if self.adjustment_active
                    else None
                ),
                "factor_cutoff_rule": "effective_date <= data_cutoff",
            },
            "pit_status": self.pit_status(),
            "instrument_mapping_summary": mapping_summary,
            "coverage": {
                "daily_bars": {
                    str(instrument_id): self.coverage_summary(
                        expected_sessions, bars_by_instrument.get(instrument_id, ())
                    )
                    for instrument_id in instrument_ids
                },
                **(
                    {"adjusted_series": {"coverage": factor_coverage}}
                    if factor_coverage
                    else {}
                ),
            },
            "source_revisions": {
                "daily_bars": {
                    "source": self.source,
                    "latest_observed_at": self.revision_stamp(daily_rows),
                },
                "adjustment_factors": {
                    "source": self.source,
                    "latest_observed_at": self.revision_stamp(factor_rows),
                },
            },
            "issues": [dict(issue) for issue in blocking_issues],
        }
        summary["report_hash"] = canonical_hash(summary)
        return summary


def build_data_preflight_payloads(
    summary: Mapping[str, object],
) -> dict[str, object]:
    """Map an adapter preflight summary onto the persisted record fields.

    The existing ``backtest_data_preflight`` record already carries JSON
    payload columns; no new ETF-specific table or column is created.  The
    mapping summaries ride inside the ``coverage`` payload.
    """

    pit_status = summary.get("pit_status")
    pit_value = ""
    if isinstance(pit_status, Mapping):
        # Only families explicitly declared "non_strict" trip the run-level
        # flag.  Policy markers such as
        # "tushare_adj_factor_native@1:effective_date_cutoff" are contract
        # declarations, not missing knowledge-time evidence: the approved
        # first-version factor contract never triggers non_strict_pit.
        non_strict_families = sorted(
            family
            for family, value in pit_status.items()
            if value == "non_strict"
        )
        pit_value = (
            "strict"
            if not non_strict_families
            else f"non_strict:{','.join(non_strict_families)}"
        )
    coverage_payload: dict[str, object] = dict(
        summary.get("coverage") or {}
    )
    # Audit summaries ride the coverage payload of the existing record so
    # /data-preflight can explain which code mappings were used and on
    # what evidence the adjustment policy was activated.
    if summary.get("instrument_mapping_summary"):
        coverage_payload["instrument_mapping_summary"] = summary[
            "instrument_mapping_summary"
        ]
    if summary.get("adjustment_series_validation"):
        coverage_payload["adjustment_series_validation"] = summary[
            "adjustment_series_validation"
        ]
    issues = summary.get("issues", [])
    failure_reason = None
    if issues:
        first = issues[0]
        failure_reason = str(first.get("code", "history_incomplete"))
    return {
        "capabilities": {
            "provider_key": summary.get("provider_key"),
            "data_contract_version": summary.get("data_contract_version"),
            "adjustment_series_policy": summary.get("adjustment_series_policy"),
        },
        "coverage": coverage_payload,
        "pit_status": pit_value,
        "source_revisions": summary.get("source_revisions"),
        "session_summary": {
            "failure_reason": failure_reason,
        },
    }
