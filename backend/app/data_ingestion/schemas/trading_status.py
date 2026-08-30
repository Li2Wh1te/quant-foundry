"""Normalization for Tushare daily suspension facts."""
from dataclasses import dataclass
from datetime import date
from typing import Any

@dataclass(frozen=True, slots=True)
class TradingStatusInput:
    ts_code: str
    trade_date: date
    status: str
    source: str = "tushare"
    raw: dict[str, Any] | None = None

def normalize_suspend_row(row: dict[str, Any]) -> TradingStatusInput:
    code = str(row.get("ts_code") or "").strip()
    value = row.get("trade_date")
    if not code or value in (None, ""):
        raise ValueError("suspend row requires ts_code and trade_date")
    day = value if isinstance(value, date) else date.fromisoformat(str(value))
    kind = str(row.get("suspend_type") or "S").strip().upper()
    status = "suspended" if kind in {"S", "SUSPENDED", "停牌"} else "tradable"
    return TradingStatusInput(code, day, status, raw=dict(row))
