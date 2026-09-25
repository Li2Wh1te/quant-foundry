"""Explicit current business facts from local ingestion tables, not their audits."""
from datetime import date, datetime
from decimal import Decimal
from pydantic import BaseModel, ConfigDict, StrictStr, StrictInt, StrictBool


class Fact(BaseModel):
    model_config = ConfigDict(extra='forbid')


class CorporateAction(Fact):
    logical_fact_key: StrictStr
    instrument_id: StrictStr
    action_type: StrictStr
    record_date: date | None = None
    ex_date: date | None = None
    source_payment_date: date | None = None
    source_arrival_date: date | None = None
    cash_effective_date: date | None = None
    effective_time: datetime | None = None
    cash_effective_phase: StrictStr | None = None
    cash_amount_per_unit: Decimal | None = None
    quantity_ratio: Decimal | None = None
    quantity_delta: Decimal | None = None
    currency: StrictStr | None = None
    entitlement_rule: StrictStr | None = None
    cash_date_rule: StrictStr | None = None
    timing_rule: StrictStr | None = None
    source_revision: StrictStr | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    known_at: datetime | None = None
    observed_at: datetime | None = None
    quality: StrictStr | None = None


class TradingStatus(Fact):
    ts_code: StrictStr
    trade_date: date
    instrument_id: StrictStr | None = None
    dimension: StrictStr
    status: StrictStr
    valid_from: date | None = None
    valid_to: date | None = None
    source_revision: StrictStr | None = None
    quality_status: StrictStr
    known_at: datetime | None = None
    observed_at: datetime | None = None


class Tick(Fact):
    """Synthetic extension only: event identity is not the timestamp alone."""
    source_code: StrictStr
    trading_date: date
    session: StrictStr
    channel: StrictStr
    sequence: StrictInt
    event_ns: StrictInt
    price: Decimal
    quantity: StrictInt
    currency: StrictStr
    price_basis: StrictStr


class IntradayBar(Fact):
    source_code: StrictStr
    trading_date: date
    session: StrictStr
    frequency: StrictStr
    start_ns: StrictInt
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: StrictInt
    currency: StrictStr
    price_basis: StrictStr
