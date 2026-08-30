"""Small, fail-closed helpers for ETF adjustment-factor research reads.

The ingestion table remains the authority for factor values.  This module
only normalizes rows at the provider boundary and projects an already
verified factor series onto copied :class:`~app.backtesting.data.facts.Bar`
objects.  It deliberately has no knowledge of company actions or accounting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import (
    Decimal,
    InvalidOperation,
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
    ROUND_05UP,
)
import re
from typing import Iterable, Mapping, Sequence
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.backtesting.data.errors import (
    HistoryBarsDuplicateError,
    HistoryBarsIncompleteError,
    InvalidDataRequestError,
    ProviderContractViolationError,
)
from app.backtesting.data.facts import AdjustedSeriesPoint, Bar
from app.backtesting.data.requests import PriceBasis

__all__ = [
    "NormalizedAdjustmentFactor",
    "normalize_adjustment_factor",
    "normalize_adjustment_factors",
    "build_research_price_series",
    "build_adjusted_price_bars",
]


_ROUNDING = {
    "ceiling": ROUND_CEILING,
    "down": ROUND_DOWN,
    "floor": ROUND_FLOOR,
    "half_down": ROUND_HALF_DOWN,
    "half_even": ROUND_HALF_EVEN,
    "half_up": ROUND_HALF_UP,
    "up": ROUND_UP,
    "05up": ROUND_05UP,
}


def _detail(value: object) -> object:
    """Convert typed coordinates to JSON-safe diagnostics."""

    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    return value


def _factor_error(message: str, **details: object) -> ProviderContractViolationError:
    """Build one stable provider-boundary error without exposing source rows."""

    return ProviderContractViolationError(
        message,
        details={key: _detail(value) for key, value in details.items()},
    )


def _parse_date(value: object, field: str) -> date:
    """Normalize source ISO/compact dates while rejecting datetimes."""

    if isinstance(value, datetime):
        raise _factor_error(f"{field} must be a calendar date", actual=value)
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        raise _factor_error(f"{field} must be a calendar date", actual=value)
    text = value.strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        if len(text) == 8 and text.isdigit():
            try:
                return datetime.strptime(text, "%Y%m%d").date()
            except ValueError:
                pass
    raise _factor_error(f"{field} must be a valid calendar date", actual=value)


def _positive_decimal(value: object, field: str) -> Decimal:
    """Normalize one exact, finite, strictly positive factor value."""

    if isinstance(value, bool) or isinstance(value, float):
        raise _factor_error(
            f"{field} must be a finite positive decimal", actual=value
        )
    if not isinstance(value, (Decimal, int, str)):
        raise _factor_error(
            f"{field} must be a finite positive decimal", actual=value
        )
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise _factor_error(
            f"{field} must be a finite positive decimal", actual=value
        ) from exc
    if not result.is_finite() or result <= 0:
        raise _factor_error(
            f"{field} must be a finite positive decimal", actual=value
        )
    return result


def cutoff_local_date(data_cutoff: datetime, timezone_name: str) -> date:
    """Return the market-local date for an aware cutoff instant."""

    if (
        not isinstance(data_cutoff, datetime)
        or data_cutoff.tzinfo is None
        or data_cutoff.utcoffset() is None
    ):
        raise InvalidDataRequestError("data_cutoff must be timezone-aware")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidDataRequestError(
            "market timezone must be a resolvable IANA timezone"
        ) from exc
    return data_cutoff.astimezone(zone).date()


@dataclass(frozen=True, slots=True)
class NormalizedAdjustmentFactor:
    """Auditable projection of one current authoritative factor row."""

    source_code: str
    source_trade_date: date
    effective_date: date
    adj_factor: Decimal
    source: str
    instrument_id: UUID
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_code, str) or not self.source_code.strip():
            raise InvalidDataRequestError("source_code must be non-blank text")
        object.__setattr__(self, "source_code", self.source_code.strip())
        if not isinstance(self.source, str) or not self.source.strip():
            raise InvalidDataRequestError("source must be non-blank text")
        object.__setattr__(self, "source", self.source.strip())
        if not isinstance(self.instrument_id, UUID):
            raise InvalidDataRequestError("instrument_id must be a UUID")
        for name in ("source_trade_date", "effective_date"):
            value = getattr(self, name)
            if not isinstance(value, date) or isinstance(value, datetime):
                raise InvalidDataRequestError(f"{name} must be a calendar date")
        object.__setattr__(
            self, "adj_factor", _positive_decimal(self.adj_factor, "adj_factor")
        )
        if self.updated_at is not None and (
            not isinstance(self.updated_at, datetime)
            or self.updated_at.tzinfo is None
            or self.updated_at.utcoffset() is None
        ):
            raise InvalidDataRequestError("updated_at must be timezone-aware")

    @property
    def point_date(self) -> date:
        """Compatibility name used by the generic adjustment-point DTO."""

        return self.effective_date

    @property
    def source_value(self) -> Decimal:
        """The exact normalized representation of the source factor value."""

        return self.adj_factor

    def as_point(self, *, price_basis: PriceBasis) -> AdjustedSeriesPoint:
        """Project this source row while retaining its date/value provenance."""

        if price_basis is PriceBasis.RAW:
            raise InvalidDataRequestError("raw prices do not have adjustment factors")
        return AdjustedSeriesPoint(
            instrument_id=self.instrument_id,
            point_date=self.effective_date,
            price_basis=price_basis,
            adj_factor=self.adj_factor,
            evidence=_factor_evidence(self),
            source_code=self.source_code,
            source_trade_date=self.source_trade_date,
        )


def _factor_evidence(row: NormalizedAdjustmentFactor):
    """Create generic evidence without treating ``updated_at`` as PIT time."""

    from app.backtesting.data.facts import FactEvidence
    from app.backtesting.data.requests import QualityStatus

    # Source rows normally carry an aware updated_at.  A deterministic
    # date-based observation timestamp is supplied for rows that do not; it
    # is evidence of observation only, never a historical cutoff.
    observed = row.updated_at
    if observed is None:
        observed = datetime.combine(
            row.effective_date, datetime.min.time(), tzinfo=UTC
        )
    return FactEvidence(
        source=row.source,
        observed_at=observed,
        quality_status=QualityStatus.COMPLETE,
        known_at=None,
    )


def normalize_adjustment_factor(
    row: object,
    *,
    instrument_id: UUID,
    source: str,
    expected_source_code: str,
    cutoff: date | None = None,
) -> NormalizedAdjustmentFactor:
    """Validate and normalize one current factor row at the read boundary."""

    source_code = getattr(row, "ts_code", None)
    if not isinstance(source_code, str) or not source_code.strip():
        raise _factor_error(
            "adjustment factor has no source code",
            expected_source_code=expected_source_code,
            actual_source_code=source_code,
            instrument_id=instrument_id,
        )
    source_code = source_code.strip()
    if source_code != expected_source_code:
        raise _factor_error(
            "adjustment factor belongs to another source code",
            expected_source_code=expected_source_code,
            actual_source_code=source_code,
            instrument_id=instrument_id,
        )
    row_source = getattr(row, "source", source)
    if not isinstance(row_source, str) or not row_source.strip() or row_source.strip() != source:
        raise _factor_error(
            "adjustment factor belongs to another source",
            expected_source=source,
            actual_source=row_source,
            source_code=source_code,
            instrument_id=instrument_id,
        )
    row_instrument = getattr(row, "instrument_id", instrument_id)
    if row_instrument != instrument_id:
        raise _factor_error(
            "adjustment factor belongs to another instrument",
            expected_instrument_id=instrument_id,
            actual_instrument_id=row_instrument,
            source_code=source_code,
        )
    source_date = _parse_date(getattr(row, "trade_date", None), "trade_date")
    explicit_effective = getattr(row, "effective_date", source_date)
    effective_date = _parse_date(explicit_effective, "effective_date")
    if effective_date != source_date:
        raise _factor_error(
            "source trade_date and normalized effective_date disagree",
            source_trade_date=source_date,
            effective_date=effective_date,
            source_code=source_code,
        )
    if cutoff is not None and effective_date > cutoff:
        raise _factor_error(
            "adjustment factor is later than data cutoff",
            source_code=source_code,
            effective_date=effective_date,
            data_cutoff=cutoff,
        )
    updated_at = getattr(row, "updated_at", None)
    return NormalizedAdjustmentFactor(
        source_code=source_code,
        source_trade_date=source_date,
        effective_date=effective_date,
        adj_factor=_positive_decimal(getattr(row, "adj_factor", None), "adj_factor"),
        source=source,
        instrument_id=instrument_id,
        updated_at=updated_at,
    )


def normalize_adjustment_factors(
    rows: Iterable[object],
    *,
    instrument_id: UUID,
    source: str,
    expected_source_code: str,
    cutoff: date | None = None,
    expected_dates: Sequence[date] | None = None,
) -> tuple[NormalizedAdjustmentFactor, ...]:
    """Normalize a complete factor batch and reject every structural defect."""

    normalized = tuple(
        normalize_adjustment_factor(
            row,
            instrument_id=instrument_id,
            source=source,
            expected_source_code=expected_source_code,
            cutoff=cutoff,
        )
        for row in rows
    )
    by_date: dict[date, NormalizedAdjustmentFactor] = {}
    for row in normalized:
        if row.effective_date in by_date:
            raise HistoryBarsDuplicateError(
                "adjustment factors contain duplicate effective_date",
                details={
                    "instrument_id": str(instrument_id),
                    "source": source,
                    "source_code": expected_source_code,
                    "effective_date": row.effective_date.isoformat(),
                },
            )
        by_date[row.effective_date] = row
    if expected_dates is not None:
        expected = tuple(expected_dates)
        expected_set = set(expected)
        actual_set = set(by_date)
        outside = sorted(actual_set - expected_set)
        missing = sorted(expected_set - actual_set)
        if outside or missing:
            raise HistoryBarsIncompleteError(
                "adjustment factors do not exactly cover requested sessions",
                details={
                    "instrument_id": str(instrument_id),
                    "source": source,
                    "source_code": expected_source_code,
                    "missing_sessions": [day.isoformat() for day in missing],
                    "out_of_window_sessions": [day.isoformat() for day in outside],
                },
            )
    return tuple(by_date[day] for day in sorted(by_date))


def _formula_kind(formula: str | None, basis: PriceBasis) -> str:
    """Resolve only the explicitly registered native formula identifiers."""

    if not isinstance(formula, str) or not formula.strip():
        raise InvalidDataRequestError(
            f"{basis.value} source-native formula is required before research reads"
        )
    # Provider artifacts use dotted prose identifiers (for example
    # ``tushare.pro_bar.native.qfq``), while older policy fixtures use the
    # compact underscore spelling.  Normalize punctuation only after the
    # exact allow-list is known; arbitrary formula names must still fail
    # closed.
    normalized = "_".join(
        part for part in re.split(r"[^a-z0-9]+", formula.strip().casefold()) if part
    )
    accepted = {
        PriceBasis.QFQ: {
            "tushare_qfq_native_v1",
            "tushare_qfq_native_1",
            "tushare_adj_factor_native_qfq_1",
            "native_qfq_1",
            "native_qfq_v1",
            "native_qfq",
            "tushare_pro_bar_native_qfq",
        },
        PriceBasis.HFQ: {
            "tushare_hfq_native_v1",
            "tushare_hfq_native_1",
            "tushare_adj_factor_native_hfq_1",
            "native_hfq_1",
            "native_hfq_v1",
            "native_hfq",
            "tushare_pro_bar_native_hfq",
        },
    }
    if normalized not in accepted[basis]:
        raise InvalidDataRequestError(
            f"unknown source-native {basis.value} formula identifier"
        )
    return normalized


def _anchor_index(anchor: str | None, basis: PriceBasis) -> str:
    """Resolve the two source-declared anchors without cross-basis fallback."""

    if not isinstance(anchor, str) or not anchor.strip():
        raise InvalidDataRequestError(
            f"{basis.value} source-native anchor is required before research reads"
        )
    normalized = "_".join(
        part for part in re.split(r"[^a-z0-9]+", anchor.strip().casefold()) if part
    )
    if basis is PriceBasis.QFQ and normalized in {
        "latest",
        "latest_factor",
        "latest_visible_close",
        "last",
        "native_end_date_latest_visible_factor",
    }:
        return "latest"
    if basis is PriceBasis.HFQ and normalized in {
        "first",
        "first_factor",
        "first_visible_close",
        "base",
    }:
        return "first"
    if basis is PriceBasis.HFQ and normalized in {
        "native_factor_at_each_effective_date",
        "factor_at_each_effective_date",
        "per_date_factor",
    }:
        # Tushare's native hfq definition is ``price * factor``.  There is
        # no fixed denominator; the factor at each effective date is the
        # scale for that date.
        return "per_date"
    raise InvalidDataRequestError(
        f"unknown source-native {basis.value} anchor identifier"
    )


def _precision(value: int | str | Mapping[str, object] | None) -> int:
    if isinstance(value, bool) or value is None:
        raise InvalidDataRequestError("source precision is required before research reads")
    if isinstance(value, int):
        places = value
    elif isinstance(value, Mapping):
        for key in ("price_decimal_places", "decimal_places", "places"):
            if key in value:
                return _precision(value[key])  # type: ignore[arg-type]
        raise InvalidDataRequestError("source precision must declare decimal places")
    elif isinstance(value, str):
        text = value.strip().casefold()
        digits = "".join(ch for ch in text if ch.isdigit())
        if not digits:
            raise InvalidDataRequestError("source precision must declare decimal places")
        places = int(digits)
    else:
        raise InvalidDataRequestError("source precision must be an integer or text")
    if places < 0 or places > 18:
        raise InvalidDataRequestError("source precision must be between 0 and 18")
    return places


def _rounding(value: str | None):
    if not isinstance(value, str) or not value.strip():
        raise InvalidDataRequestError("source rounding is required before research reads")
    text = value.strip().casefold()
    if "no local rounding" in text or "without local rounding" in text:
        return None
    key = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    for prefix in ("round_", "source_declared_", "native_"):
        if key.startswith(prefix):
            key = key[len(prefix) :]
    try:
        return _ROUNDING[key]
    except KeyError as exc:
        raise InvalidDataRequestError("unknown source rounding identifier") from exc


def build_research_price_series(
    raw_bars: Sequence[Bar],
    factors: Sequence[AdjustedSeriesPoint | NormalizedAdjustmentFactor],
    *,
    price_basis: PriceBasis,
    formula: str | None,
    anchor: str | None,
    precision: int | str | None,
    rounding: str | None,
    policy_key: str | None = None,
    policy_version: int | None = None,
) -> tuple[Bar, ...]:
    """Build qfq/hfq bars from raw bars and a complete, cutoff-visible factor set.

    The function never mutates or writes back the raw bars.  It applies the
    source-declared formula only after its identifier, anchor, precision and
    rounding have all been validated; there is intentionally no fallback
    formula or alternate price basis.
    """

    if price_basis not in (PriceBasis.QFQ, PriceBasis.HFQ):
        raise InvalidDataRequestError("research prices require qfq or hfq basis")
    _formula_kind(formula, price_basis)
    anchor_kind = _anchor_index(anchor, price_basis)
    places = _precision(precision)
    rounding_mode = _rounding(rounding)
    if not raw_bars:
        raise HistoryBarsIncompleteError("research price series has no raw bars")
    raw_rows = tuple(raw_bars)
    if any(not isinstance(row, Bar) for row in raw_rows):
        raise ProviderContractViolationError("research prices require Bar raw facts")
    instrument_id = raw_rows[0].instrument_id
    if any(row.instrument_id != instrument_id for row in raw_rows):
        raise ProviderContractViolationError("research prices contain multiple instrument identities")
    if any(row.price_basis is not PriceBasis.RAW for row in raw_rows):
        raise ProviderContractViolationError("research prices require raw input bars")
    if any(row.evidence.quality_status.value != "complete" for row in raw_rows):
        raise HistoryBarsIncompleteError("research prices require complete raw bars")
    if tuple(row.trade_date for row in raw_rows) != tuple(sorted(row.trade_date for row in raw_rows)):
        raise ProviderContractViolationError("raw bars must be in ascending date order")
    raw_dates = tuple(row.trade_date for row in raw_rows)
    if len(set(raw_dates)) != len(raw_dates):
        raise HistoryBarsDuplicateError(
            "raw bars contain duplicate trade_date values",
            details={
                "instrument_id": str(instrument_id),
                "duplicate_dates": sorted(
                    {
                        day.isoformat()
                        for day in raw_dates
                        if raw_dates.count(day) > 1
                    }
                ),
            },
        )
    raw_source_codes = {
        code
        for row in raw_rows
        for code in (getattr(row, "attributes", {}).get("source_code"),)
        if isinstance(code, str) and code.strip()
    }
    if len(raw_source_codes) > 1:
        raise ProviderContractViolationError(
            "research prices contain multiple source-code identities"
        )
    expected_source_code = next(iter(raw_source_codes), None)
    factor_rows: list[tuple[date, Decimal]] = []
    factor_provenance: dict[date, dict[str, object]] = {}
    for point in factors:
        if isinstance(point, NormalizedAdjustmentFactor):
            if point.instrument_id != instrument_id:
                raise ProviderContractViolationError("factor identity does not match raw bars")
            if expected_source_code is not None and point.source_code != expected_source_code:
                raise ProviderContractViolationError(
                    "factor source code does not match raw bars"
                )
            day, factor = point.effective_date, point.adj_factor
            factor_provenance[day] = {
                "source": point.source,
                "source_code": point.source_code,
                "source_trade_date": point.source_trade_date.isoformat(),
                "effective_date": point.effective_date.isoformat(),
                "observed_at": (
                    point.updated_at.isoformat() if point.updated_at is not None else None
                ),
                "quality_status": "complete",
            }
        elif isinstance(point, AdjustedSeriesPoint):
            if point.instrument_id != instrument_id:
                raise ProviderContractViolationError("factor identity does not match raw bars")
            if point.price_basis is not price_basis:
                raise ProviderContractViolationError(
                    "factor basis does not match requested research basis"
                )
            if (
                expected_source_code is not None
                and point.source_code is not None
                and point.source_code != expected_source_code
            ):
                raise ProviderContractViolationError(
                    "factor source code does not match raw bars"
                )
            day, factor = point.point_date, point.adj_factor
            factor_provenance[day] = {
                "source": point.evidence.source,
                "source_code": point.source_code,
                "source_trade_date": (
                    point.source_trade_date.isoformat()
                    if point.source_trade_date is not None
                    else day.isoformat()
                ),
                "effective_date": day.isoformat(),
                "observed_at": point.evidence.observed_at.isoformat(),
                "known_at": (
                    point.evidence.known_at.isoformat()
                    if point.evidence.known_at is not None
                    else None
                ),
                "quality_status": point.evidence.quality_status.value,
                "source_revision": point.evidence.source_revision,
            }
        else:
            raise ProviderContractViolationError("research prices require adjustment factor facts")
        if day in {item[0] for item in factor_rows}:
            raise HistoryBarsDuplicateError(
                "research factors contain duplicate effective_date",
                details={"effective_date": day.isoformat()},
            )
        factor_rows.append((day, factor))
    factor_map = dict(factor_rows)
    expected_days = {row.trade_date for row in raw_rows}
    outside = sorted(factor_map.keys() - expected_days)
    if outside:
        raise HistoryBarsIncompleteError(
            "research prices received adjustment factors outside the raw window",
            details={"out_of_window_sessions": [day.isoformat() for day in outside]},
        )
    missing = sorted(expected_days - factor_map.keys())
    if missing:
        raise HistoryBarsIncompleteError(
            "research prices are missing cutoff-visible adjustment factors",
            details={"missing_sessions": [day.isoformat() for day in missing]},
        )
    anchor_date = max(factor_map) if anchor_kind == "latest" else min(factor_map)
    anchor_factor = factor_map[anchor_date]
    if anchor_factor <= 0 or not anchor_factor.is_finite():
        raise ProviderContractViolationError("research anchor factor must be finite and positive")
    quantum = Decimal(1).scaleb(-places)
    result: list[Bar] = []
    for raw in raw_rows:
        if anchor_kind == "per_date":
            scale = factor_map[raw.trade_date]
        else:
            scale = factor_map[raw.trade_date] / anchor_factor
        values = {
            field: (
                (getattr(raw, field) * scale)
                if rounding_mode is None
                else (getattr(raw, field) * scale).quantize(
                    quantum, rounding=rounding_mode
                )
            )
            for field in ("open", "high", "low", "close")
        }
        result.append(
            Bar(
                instrument_id=raw.instrument_id,
                trade_date=raw.trade_date,
                frequency=raw.frequency,
                open=values["open"],
                high=values["high"],
                low=values["low"],
                close=values["close"],
                volume=raw.volume,
                amount=raw.amount,
                price_basis=price_basis,
                evidence=raw.evidence,
                schema=raw.schema,
                validation_rule_version=raw.validation_rule_version,
                attributes={
                    **dict(raw.attributes),
                    "research_price_basis": price_basis.value,
                    "adjustment_series_policy": {
                        "key": policy_key,
                        "version": policy_version,
                    },
                    "adjustment_factor": str(factor_map[raw.trade_date]),
                    "adjustment_factor_provenance": factor_provenance[raw.trade_date],
                    "adjustment_anchor": anchor_kind,
                    "adjustment_formula": formula,
                    "adjustment_precision": places,
                    "adjustment_rounding": rounding,
                },
            )
        )
    return tuple(result)


build_adjusted_price_bars = build_research_price_series
