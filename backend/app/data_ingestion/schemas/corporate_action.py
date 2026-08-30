"""Schemas and normalization helpers for Tushare fund_div."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib, json
from typing import Any

@dataclass(frozen=True)
class CorporateActionInput:
    ts_code: str
    ann_date: date | None
    record_date: date | None
    ex_date: date | None
    payment_date: date | None
    amount: Decimal | None
    currency: str | None
    status: str | None
    raw: dict[str, Any]
    source_hash: str
    source_payment_date_raw: date | None = None
    source_arrival_date_raw: date | None = None
    status_raw: str | None = None
    status_quality: str = "invalid"
    cash_date_rule: str | None = None

def _d(v: Any) -> date | None:
    if v in (None, ""): return None
    try: return date.fromisoformat(str(v).replace("/", "-"))
    except ValueError: return None

def _dec(v: Any) -> Decimal | None:
    if v in (None, ""): return None
    try: return Decimal(str(v))
    except (InvalidOperation, ValueError): return None

def normalize_fund_div_row(row: dict[str, Any]) -> CorporateActionInput:
    raw = dict(row)
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()
    pay, arrival = _d(row.get("pay_date")), _d(row.get("earpay_date"))
    # T18-00 freezes earpay_date as preferred arrival date, with pay_date fallback.
    selected = arrival or pay
    raw_status = row.get("div_proc") if row.get("div_proc") is not None else row.get("status")
    status_text = str(raw_status).strip() if raw_status is not None else None
    status_map = {"实施": "implemented", "已实施": "implemented", "实施完成": "implemented"}
    mapped = status_map.get(status_text)
    return CorporateActionInput(
        ts_code=str(row.get("ts_code", "")).strip(), ann_date=_d(row.get("ann_date")),
        record_date=_d(row.get("record_date")), ex_date=_d(row.get("ex_date")),
        payment_date=selected,
        amount=_dec(row.get("div_cash") or row.get("cash_div")),
        currency=(str(row.get("currency")).strip() if row.get("currency") else None),
        status=mapped, raw=raw, source_hash=digest,
        source_payment_date_raw=pay, source_arrival_date_raw=arrival,
        status_raw=status_text, status_quality="valid" if mapped else "invalid",
        cash_date_rule="earpay_date_preferred_pay_date_fallback@tushare_fund_div_cash_date@1" if selected else None)
