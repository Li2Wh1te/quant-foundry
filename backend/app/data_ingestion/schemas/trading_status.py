"""Normalization for Tushare daily suspension facts."""
from dataclasses import dataclass
from datetime import date
import hashlib
import json
from typing import Any


@dataclass(frozen=True, slots=True)
class TradingStatusInput:
    """One normalized status row with a stable source-content revision."""

    ts_code: str
    trade_date: date
    status: str
    source: str = "tushare"
    raw: dict[str, Any] | None = None
    source_revision: str | None = None
    quality_status: str = "complete"


def normalize_suspend_row(row: dict[str, Any]) -> TradingStatusInput:
    """Normalize one source row without treating unknown values as tradable."""

    code = str(row.get("ts_code") or "").strip()
    value = row.get("trade_date")
    if not code or value in (None, ""):
        raise ValueError("suspend row requires ts_code and trade_date")
    day = value if isinstance(value, date) else date.fromisoformat(str(value))
    kind = str(row.get("suspend_type") or "").strip().upper()
    if kind in {"S", "SUSPENDED", "停牌"}:
        status, quality = "suspended", "complete"
    elif kind in {"R", "RESUMED", "TRADABLE", "复牌"}:
        status, quality = "tradable", "complete"
    else:
        status, quality = "unknown", "invalid"
    raw = dict(row)
    digest = hashlib.sha256(
        json.dumps(raw, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return TradingStatusInput(
        code,
        day,
        status,
        raw=raw,
        source_revision=f"derived:tushare:suspend_d_row@1:sha256:{digest}",
        quality_status=quality,
    )
