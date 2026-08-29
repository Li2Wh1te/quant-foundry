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
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Mapping, Protocol, Sequence
from uuid import UUID

from app.backtesting.calendar_axis import CalendarSnapshot, SessionPoint, normalize_calendar_id
from app.backtesting.data.errors import (
    HistoryIncompleteError,
    IdentityMappingConflictError,
    IdentityMappingIncompleteError,
    InstrumentCalendarUnresolvedError,
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
    InstrumentCodeMapping,
    InstrumentDisplay,
    InstrumentSpec,
    InstrumentSpecProvider,
    MappingConflictError,
    MappingCoverageGapError,
)

__all__ = [
    "ADJUSTMENT_SERIES_POLICY",
    "ETF_ADAPTER_KEY",
    "ETF_ADAPTER_VERSION",
    "ETF_VALIDATION_RULE_KEY",
    "ETF_VALIDATION_RULE_VERSION",
    "ETF_PROVIDER_KEY",
    "ETF_RULE_PACKAGE",
    "EtfFactsAdapter",
    "build_data_preflight_payloads",
]


ETF_PROVIDER_KEY = "etf_ingestion"
"""Stable provider key declared by the ETF ingestion data foundation."""

# These identifiers are intentionally owned by the adapter rather than the
# engine.  Persisting the identifiers in preflight evidence makes a replay
# auditable when the ETF-specific legality rules evolve.
ETF_ADAPTER_KEY = "etf_raw_bar_adapter"
ETF_ADAPTER_VERSION = "etf_raw_bar_adapter@1"
ETF_VALIDATION_RULE_KEY = "etf_raw_bar_validation"
ETF_VALIDATION_RULE_VERSION = "etf_raw_bar_validation@1"

ETF_RULE_PACKAGE = ("china_listed_etf_rules", 1)
"""Versioned rule package identifier used by the instrument domain."""

ADJUSTMENT_SERIES_POLICY = ("tushare_adj_factor_native", 1)
"""First-version adjustment-series policy (key, version)."""

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
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    vol: Decimal | None
    amount: Decimal | None
    updated_at: datetime


class AdjustmentFactorRow(Protocol):
    """Structural view of one ``etf_adjustment_factors`` row."""

    ts_code: str
    trade_date: date
    adj_factor: Decimal
    updated_at: datetime


def _decimal_or_none(value: object) -> Decimal | None:
    """Read a finite source value for validation without changing its value."""

    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, float):
        # ``project_bar`` will reject these at the generic fact boundary.  A
        # validation issue still gives preflight a useful, JSON-safe reason.
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _raw_value(value: object) -> str | None:
    """Serialize one raw value without introducing binary floating point."""

    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _bar_validation_issues(
    row: DailyBarRow,
    *,
    instrument_id: UUID | None = None,
    source: str = ETF_PROVIDER_KEY,
    source_code: str | None = None,
) -> tuple[dict[str, object], ...]:
    """Return one JSON-safe issue per failed ETF v1 OHLC rule.

    This function only observes source values.  It never repairs, drops, or
    replaces an invalid value; callers may therefore retain the raw row for
    an auditable preflight report while withholding it from execution.
    """

    code = source_code if source_code is not None else getattr(row, "ts_code", None)
    common: dict[str, object] = {
        "instrument_id": str(instrument_id) if instrument_id is not None else None,
        "trade_date": (
            row.trade_date.isoformat()
            if isinstance(getattr(row, "trade_date", None), date)
            else None
        ),
        "source": source,
        "source_code": code,
        "rule_key": ETF_VALIDATION_RULE_KEY,
        "rule_version": ETF_VALIDATION_RULE_VERSION,
        "adapter_key": ETF_ADAPTER_KEY,
        "adapter_version": ETF_ADAPTER_VERSION,
    }
    issues: list[dict[str, object]] = []

    def add(field: str, raw: object, reason: str, *, code_name: str) -> None:
        issues.append(
            {
                **common,
                "field": field,
                "raw_value": _raw_value(raw),
                "reason": reason,
                "code": code_name,
            }
        )

    values = {
        field: getattr(row, field, None)
        for field in ("open", "high", "low", "close")
    }
    parsed = {field: _decimal_or_none(value) for field, value in values.items()}
    for field, value in values.items():
        if value is None:
            add(field, value, "missing_ohlc_field", code_name="bar_field_missing")
            continue
        numeric = parsed[field]
        if numeric is None:
            add(field, value, "invalid_decimal", code_name="bar_invalid")
        elif numeric <= 0:
            add(field, value, "non_positive_price", code_name="bar_invalid")

    high, low = parsed["high"], parsed["low"]
    if high is not None and low is not None and high < low:
        add("high", values["high"], "high_below_low", code_name="bar_invalid")
    for field in ("open", "close"):
        value = parsed[field]
        if (
            value is not None
            and value > 0
            and low is not None
            and high is not None
            and low <= high
            and not (low <= value <= high)
        ):
            add(field, values[field], "price_outside_low_high", code_name="bar_invalid")
    return tuple(issues)


def _bar_quality(row: DailyBarRow) -> QualityStatus:
    """Classify a raw bar row without repairing anything.

    A row is consumable (``complete``) only when every OHLC price is strictly
    positive and satisfies the ETF range rules.  Volume and amount have no
    business-validity rule in this task and are therefore preserved as-is.
    Everything else stays an explicit ``invalid`` fact with its original
    values so coverage and preflight can block on it.
    """

    return (
        QualityStatus.INVALID
        if _bar_validation_issues(row)
        else QualityStatus.COMPLETE
    )


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
    adjustment_active: bool = False
    adjustment_verification_evidence: str | None = None
    # Task-11 canonical identity hook: the resolver must receive the
    # effective day and PIT cutoff so it can return
    # InstrumentIdentityFact.calendar_id.  There is no adapter-level
    # exchange/calendar fallback.
    calendar_id_resolver: Callable[..., str | None] | None = None
    # Trading rules are resolved by the instrument domain.  Keeping this
    # port optional preserves the adapter's read-only bar APIs while making
    # an ETF spec impossible to fabricate from the ingestion directory.
    spec_provider: InstrumentSpecProvider | None = None
    # Descriptive alias for callers that prefer the protocol's full name.
    instrument_spec_provider: InstrumentSpecProvider | None = None

    def __post_init__(self) -> None:
        if self.calendar_id_resolver is not None and not callable(self.calendar_id_resolver):
            raise InvalidDataRequestError("calendar_id_resolver must be callable")
        providers = tuple(
            provider
            for provider in (self.spec_provider, self.instrument_spec_provider)
            if provider is not None
        )
        if len(providers) == 2 and providers[0] is not providers[1]:
            raise InvalidDataRequestError(
                "spec_provider and instrument_spec_provider must refer to one provider"
            )
        for provider in providers:
            if not callable(getattr(provider, "resolve_spec", None)):
                raise InvalidDataRequestError(
                    "spec provider must expose a callable resolve_spec method"
                )
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

    def _identity_calendar_id(
        self,
        row: object,
        instrument_id: UUID,
        *,
        effective_date: date,
        data_cutoff: datetime | None,
    ) -> str | None:
        """Read one PIT calendar from an identity fact or strict resolver.

        A plain ``row.calendar_id`` attribute is intentionally not accepted:
        an ETF code row is not an identity fact and does not carry the
        effective-day/PIT evidence required by task 11.  Callers may provide
        an already-resolved ``identity_fact`` sidecar, or production wiring
        may inject a resolver with the explicit identity/PIT arguments.
        """

        # Identity resolution always uses the caller's frozen PIT cutoff;
        # wall-clock fallbacks would make a replay non-deterministic.
        cutoff = data_cutoff
        if cutoff is None:
            return None
        try:
            cutoff = _aware_datetime(cutoff, "data_cutoff")
        except Exception as exc:
            raise InstrumentCalendarUnresolvedError(
                "ETF calendar resolution requires an aware data_cutoff",
                details={"instrument_id": str(instrument_id)},
            ) from exc

        identity_fact = getattr(row, "identity_fact", None)
        if identity_fact is not None:
            if getattr(identity_fact, "instrument_id", instrument_id) != instrument_id:
                raise InstrumentCalendarUnresolvedError(
                    "ETF identity fact does not belong to the requested instrument",
                    details={"instrument_id": str(instrument_id)},
                )
            valid_from = getattr(identity_fact, "valid_from", None)
            valid_to = getattr(identity_fact, "valid_to", None)
            known_at = getattr(identity_fact, "known_at", None)
            if (
                not isinstance(valid_from, date)
                or isinstance(valid_from, datetime)
                or (valid_to is not None and (not isinstance(valid_to, date) or isinstance(valid_to, datetime)))
                or not isinstance(known_at, datetime)
                or known_at.tzinfo is None
                or known_at.utcoffset() is None
                or known_at > cutoff
                or effective_date < valid_from
                or (valid_to is not None and effective_date >= valid_to)
            ):
                raise InstrumentCalendarUnresolvedError(
                    "ETF identity fact is not visible for the requested effective day and PIT cutoff",
                    details={
                        "instrument_id": str(instrument_id),
                        "effective_date": effective_date.isoformat(),
                        "data_cutoff": cutoff.isoformat(),
                    },
                )
            calendar_id = getattr(identity_fact, "calendar_id", None)
        elif self.calendar_id_resolver is not None:
            try:
                calendar_id = self.calendar_id_resolver(
                    instrument_id,
                    effective_date=effective_date,
                    data_cutoff=cutoff,
                )
            except TypeError as exc:
                raise InstrumentCalendarUnresolvedError(
                    "ETF calendar resolver must accept effective_date and data_cutoff",
                    details={"instrument_id": str(instrument_id)},
                ) from exc
        else:
            return None
        if not isinstance(calendar_id, str) or not calendar_id.strip():
            return None
        try:
            return normalize_calendar_id(calendar_id)
        except Exception as exc:
            raise InstrumentCalendarUnresolvedError(
                "ETF identity fact returned an invalid calendar_id",
                details={
                    "instrument_id": str(instrument_id),
                    "calendar_id": calendar_id,
                },
            ) from exc

    def project_instrument_spec(
        self,
        row: object,
        *,
        effective_date: date | None = None,
        data_cutoff: datetime | None = None,
    ) -> InstrumentSpec | None:
        """Project one ``etf_codes`` row onto a complete engine spec.

        The ingestion directory only supplies a stable identity/display
        candidate.  A complete spec must come from the injected instrument
        domain provider, which resolves PIT identity and versioned rule facts;
        no adapter-level trading, currency, exchange, calendar, session, or
        capability defaults are permitted.  The validity window starts
        strictly at ``list_date``: falling back to ``setup_date`` would admit
        funds that exist but are not listed yet.
        """

        display = self.project_display(row)
        if display is None:
            return None
        list_date = getattr(row, "list_date", None)
        if effective_date is None:
            effective_date = list_date
        if not isinstance(effective_date, date) or isinstance(effective_date, datetime):
            return None
        provider = self.spec_provider or self.instrument_spec_provider
        if provider is None:
            # The directory row is not a rules fact.  Without the domain
            # provider there is no safe projection, so fail closed with the
            # stable calendar/spec-resolution contract error.  Rows that do
            # not even expose the legacy exchange marker remain unresolvable
            # ``None`` for compatibility with display-only callers.
            if not isinstance(getattr(row, "exchange", None), str) or not getattr(
                row, "exchange", ""
            ).strip():
                return None
            raise InstrumentCalendarUnresolvedError(
                "ETF instrument spec requires an InstrumentSpecProvider",
                details={
                    "instrument_id": str(display.instrument_id),
                    "ts_code": getattr(row, "ts_code", None),
                },
            )
        if data_cutoff is None:
            raise InvalidDataRequestError(
                "data_cutoff is required for point-in-time ETF spec resolution"
            )
        try:
            cutoff = _aware_datetime(data_cutoff, "data_cutoff")
        except Exception as exc:
            raise InvalidDataRequestError(
                "data_cutoff must be a timezone-aware datetime"
            ) from exc
        effective_at = datetime.combine(effective_date, datetime.min.time(), tzinfo=timezone.utc)
        spec = provider.resolve_spec(
            display.instrument_id,
            effective_at=effective_at,
            data_cutoff=cutoff,
        )
        if spec is None:
            return None
        if not isinstance(spec, InstrumentSpec):
            raise ProviderContractViolationError(
                "instrument spec provider returned a non-InstrumentSpec value",
                details={"instrument_id": str(display.instrument_id)},
            )
        if spec.instrument_id != display.instrument_id:
            raise ProviderContractViolationError(
                "instrument spec provider returned another instrument identity",
                details={
                    "requested_instrument_id": str(display.instrument_id),
                    "returned_instrument_id": str(spec.instrument_id),
                },
            )
        return spec

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
        source_code = getattr(row, "ts_code", None)
        missing_reasons = {
            ("volume" if field == "vol" else field): "source_field_missing"
            for field in ("open", "high", "low", "close", "vol", "amount")
            if getattr(row, field, None) is None
        }
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
            validation_rule_version=ETF_VALIDATION_RULE_VERSION,
            attributes={
                "source_code": source_code,
                "field_units": {
                    "open": "CNY",
                    "high": "CNY",
                    "low": "CNY",
                    "close": "CNY",
                    "volume": "lot",
                    "amount": "thousand_CNY",
                },
                "missing_reasons": missing_reasons,
                "adapter_key": ETF_ADAPTER_KEY,
                "adapter_version": ETF_ADAPTER_VERSION,
                "validation_rule_version": ETF_VALIDATION_RULE_VERSION,
            },
        )

    def validate_bar(
        self,
        row: DailyBarRow | Bar,
        instrument_id: UUID | None = None,
        *,
        source_code: str | None = None,
    ) -> tuple[dict[str, object], ...]:
        """Return JSON-safe ETF v1 legality issues for one raw/projection row."""

        if isinstance(row, Bar):
            instrument_id = instrument_id or row.instrument_id
            source = row.evidence.source
            source_code = source_code or getattr(row, "attributes", {}).get("source_code")
        else:
            source = self.source
        return _bar_validation_issues(
            row,
            instrument_id=instrument_id,
            source=source,
            source_code=source_code,
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

    def preflight_bars(
        self,
        instrument_id: UUID,
        *,
        resolution: PITMappingResolution,
    ) -> dict[str, object]:
        """Read raw bars for admission checks, retaining invalid facts.

        ``bars()`` deliberately applies the generic complete-quality gate
        before formal consumption.  Admission preflight needs the opposite
        view: an invalid source row must be visible as evidence, not silently
        look like a missing row.  This method therefore performs only the
        structural checks needed to build a report and never returns a row to
        strategy code.
        """

        if resolution.instrument_id != instrument_id:
            raise ProviderContractViolationError(
                "bar preflight resolution belongs to another instrument",
                details={
                    "instrument_id": str(instrument_id),
                    "resolution_instrument_id": str(resolution.instrument_id),
                },
            )
        returned_dates: list[date] = []
        out_of_window: list[date] = []
        duplicate_dates: list[date] = []
        seen: set[date] = set()
        invalid_bars: list[dict[str, object]] = []
        for segment in resolution.segments:
            for raw in self.daily_bars(
                segment.source_code,
                segment.first_requested_session,
                segment.last_requested_session,
            ):
                self.require_row_code(raw, segment.source_code)
                bar = self.project_bar(raw, instrument_id)
                returned_dates.append(bar.trade_date)
                if bar.trade_date in seen:
                    duplicate_dates.append(bar.trade_date)
                seen.add(bar.trade_date)
                if bar.trade_date not in segment.requested_sessions:
                    out_of_window.append(bar.trade_date)
                invalid_bars.extend(self.validate_bar(bar, instrument_id))
        expected = list(resolution.requested_sessions)
        missing = sorted(set(expected) - set(returned_dates))
        structurally_complete = not (missing or duplicate_dates or out_of_window)
        status = "ready" if structurally_complete and not invalid_bars else "blocked"
        return {
            "status": status,
            "instrument_id": str(instrument_id),
            "source": resolution.source,
            "frequency": "1d",
            "price_basis": PriceBasis.RAW.value,
            "requested_range": {
                "start_date": expected[0].isoformat() if expected else None,
                "end_date": expected[-1].isoformat() if expected else None,
            },
            "expected_sessions": len(expected),
            "returned_sessions": len(set(returned_dates)),
            "missing_sessions": [day.isoformat() for day in missing],
            "duplicate_sessions": sorted({day.isoformat() for day in duplicate_dates}),
            "out_of_window_sessions": sorted({day.isoformat() for day in out_of_window}),
            "mapping_segments": [
                {
                    "source_code": segment.source_code,
                    "first_session": segment.first_requested_session.isoformat(),
                    "last_session": segment.last_requested_session.isoformat(),
                    "requested_sessions": [day.isoformat() for day in segment.requested_sessions],
                    "fact_id": str(segment.fact_id) if segment.fact_id else None,
                    "fact_version": segment.fact_version,
                    "mapping_evidence": segment.mapping.evidence,
                }
                for segment in resolution.segments
            ],
            "invalid_bars": invalid_bars,
            "adapter_key": ETF_ADAPTER_KEY,
            "adapter_version": ETF_ADAPTER_VERSION,
            "rule_key": ETF_VALIDATION_RULE_KEY,
            "rule_version": ETF_VALIDATION_RULE_VERSION,
        }

    def bar_validity_summary(
        self,
        rows: Sequence[DailyBarRow | Bar],
        *,
        instrument_id: UUID | None = None,
    ) -> dict[str, object]:
        """Summarize ETF v1 validity without dropping raw source rows."""

        invalid_bars: list[dict[str, object]] = []
        for row in rows:
            invalid_bars.extend(self.validate_bar(row, instrument_id))
        return {
            "status": "blocked" if invalid_bars else "ready",
            "adapter_key": ETF_ADAPTER_KEY,
            "adapter_version": ETF_ADAPTER_VERSION,
            "rule_key": ETF_VALIDATION_RULE_KEY,
            "rule_version": ETF_VALIDATION_RULE_VERSION,
            "invalid_count": len({
                (item.get("instrument_id"), item.get("trade_date"))
                for item in invalid_bars
            }),
            "invalid_field_count": len(invalid_bars),
            "invalid_bars": invalid_bars,
        }

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

    def session_points(
        self,
        start_date: date,
        end_date: date,
        *,
        calendar_id: str | None = None,
        snapshot: CalendarSnapshot | None = None,
        instrument_id: UUID | None = None,
        data_cutoff: datetime | None = None,
    ) -> tuple[SessionPoint, ...]:
        """Project sessions from an immutable PIT calendar snapshot.

        Callers must supply the CalendarSnapshot opened for the
        identity-derived calendar; it is the only source that can prove PIT
        session definitions.
        """

        if snapshot is not None:
            if start_date < snapshot.request.formal_start or end_date > snapshot.request.formal_end:
                raise ProviderContractViolationError(
                    "calendar snapshot does not cover requested session range"
                )
            points = tuple(
                point for point in snapshot.resolution.resolved_sessions
                if start_date <= point.session_date <= end_date
            )
            for point in points:
                context = point.context
                if context is None or not context.calendar_ids:
                    raise ProviderContractViolationError(
                        "ETF session points require instrument/calendar snapshot context"
                    )
                if instrument_id is not None and instrument_id not in context.instrument_ids:
                    raise ProviderContractViolationError(
                        "session point context does not include the requested instrument"
                    )
                if calendar_id is not None and normalize_calendar_id(calendar_id) not in context.calendar_ids:
                    raise ProviderContractViolationError(
                        "session point context does not include the requested calendar"
                    )
            return points
        # Calendar windows and timezone are facts of the immutable snapshot;
        # the adapter cannot synthesize them from an exchange or code.
        raise ProviderContractViolationError(
            "ETF session projection requires an immutable CalendarSnapshot"
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
        requested_range: Mapping[str, object] | None = None,
        lookback_sessions: int | None = None,
        max_lookback_sessions: int = 512,
    ) -> dict[str, object]:
        """Assemble the machine summary consumed by result records.

        The content deliberately excludes generation time, database keys,
        and credentials so identical data facts hash identically; the hash
        changes when source revisions, coverage, mappings, or the
        adjustment-policy activation state change.
        """

        # Keep one issue record per failed field.  This gives the report a
        # precise raw value while ``bar_validity_summary`` below provides the
        # bar-level counts operators need at a glance.
        invalid_bars: list[dict[str, object]] = []
        for row in daily_rows:
            row_instrument_id = getattr(row, "instrument_id", None)
            if row_instrument_id is None and len(instrument_ids) == 1:
                row_instrument_id = instrument_ids[0]
            invalid_bars.extend(
                self.validate_bar(row, row_instrument_id)
            )

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
        expected_range = dict(requested_range or {})
        if not expected_range and expected_sessions:
            expected_range = {
                "start_date": min(expected_sessions).isoformat(),
                "end_date": max(expected_sessions).isoformat(),
            }
        issues_payload = [dict(issue) for issue in blocking_issues]
        # Invalid bars are blocking issues, but remain separately indexed in
        # ``invalid_bars`` so callers can render the original field/value.
        issues_payload.extend(
            {
                "code": item.get("code", "bar_invalid"),
                "instrument_id": item.get("instrument_id"),
                "trade_date": item.get("trade_date"),
                "field": item.get("field"),
                "reason": item.get("reason"),
            }
            for item in invalid_bars
        )
        summary: dict[str, object] = {
            "provider_key": ETF_PROVIDER_KEY,
            "adapter_key": ETF_ADAPTER_KEY,
            "adapter_version": ETF_ADAPTER_VERSION,
            "validation_rule_version": ETF_VALIDATION_RULE_VERSION,
            "data_contract_version": 1,
            "capability": "bars",
            "frequency": "1d",
            "price_basis": PriceBasis.RAW.value,
            "requested_range": expected_range,
            "lookback_sessions": lookback_sessions,
            "max_lookback_sessions": max_lookback_sessions,
            "expected_sessions": len(expected_sessions),
            "returned_sessions": sum(
                len(set(bars_by_instrument.get(instrument_id, ())))
                for instrument_id in instrument_ids
            ),
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
            "mapping_segments": mapping_summary,
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
            "invalid_bars": invalid_bars,
            "bar_validity_summary": {
                "adapter_key": ETF_ADAPTER_KEY,
                "adapter_version": ETF_ADAPTER_VERSION,
                "rule_key": ETF_VALIDATION_RULE_KEY,
                "rule_version": ETF_VALIDATION_RULE_VERSION,
                "invalid_count": len({
                    (item.get("instrument_id"), item.get("trade_date"))
                    for item in invalid_bars
                }),
                "invalid_field_count": len(invalid_bars),
                "blocked": bool(invalid_bars),
                "invalid_bars": invalid_bars,
            },
            "issues": issues_payload,
        }
        coverage_values = [
            self.coverage_summary(expected_sessions, bars_by_instrument.get(instrument_id, ()))
            for instrument_id in instrument_ids
        ]
        summary["status"] = (
            "blocked"
            if invalid_bars
            or blocking_issues
            or any(item["status"] != "complete" for item in coverage_values)
            else "ready"
        )
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
    for field in ("bar_validity_summary", "invalid_bars"):
        if summary.get(field) is not None:
            coverage_payload[field] = summary[field]
    issues = summary.get("issues", [])
    failure_reason = None
    if issues:
        first = issues[0]
        failure_reason = str(first.get("code", "history_incomplete"))
    elif summary.get("invalid_bars"):
        first_invalid = summary["invalid_bars"][0]
        failure_reason = str(first_invalid.get("code", "bar_invalid"))
    return {
        "capabilities": {
            "provider_key": summary.get("provider_key"),
            "data_contract_version": summary.get("data_contract_version"),
            "adapter_key": summary.get("adapter_key"),
            "adapter_version": summary.get("adapter_version"),
            "validation_rule_version": summary.get("validation_rule_version"),
            "adjustment_series_policy": summary.get("adjustment_series_policy"),
        },
        "coverage": coverage_payload,
        "pit_status": pit_value,
        "source_revisions": summary.get("source_revisions"),
        "session_summary": {
            "failure_reason": failure_reason,
        },
    }
