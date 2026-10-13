"""Public business DTOs shared by the S2 market modules.

Exact prices/quantities are decimal text; technical nodes stay inside the reader.
"""
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

DecimalText = Annotated[str, Field(pattern=r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$", max_length=256)]
Availability = Literal['available', 'restricted', 'unavailable', 'unknown']
AssetType = Literal['etf', 'stock', 'index']
Period = Literal['day', 'week', 'month', 'year']
Adjustment = Literal['none', 'pre', 'post']


class DTO(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Notice(DTO):
    code: str
    message: str
    scope: str | None = None


class Capability(DTO):
    name: str
    availability: Availability
    request_checked: bool
    limitations: list[Notice] = Field(default_factory=list)
    # Mapping support, known current metadata and request acceptance are separate.
    mapping_implemented: bool = True
    metadata_known: bool = False


class Coverage(DTO):
    verified: bool = False
    state: Literal['verified', 'partial', 'unknown'] = 'unknown'
    missing_dates: list[date] | None = None
    note: str = '尚未核验本次业务日期覆盖。'


class ReadMeta(DTO):
    read_token: str
    read_at: datetime
    business_as_of: date | None = None
    scope: str
    source_labels: list[str]
    coverage: Coverage = Field(default_factory=Coverage)
    limitations: list[Notice] = Field(default_factory=list)


class CatalogOption(DTO):
    catalog_key: str
    label: str
    asset_type: AssetType
    capability: Capability


class CapabilitiesResult(DTO):
    catalogs: list[CatalogOption]
    capabilities: list[Capability]
    read_at: datetime


class InstrumentResolution(DTO):
    instrument_key: str
    symbol: str
    asset_type: AssetType
    meta: ReadMeta


class Instrument(DTO):
    instrument_key: str
    symbol: str
    name: str | None = None
    asset_type: AssetType
    exchange: str | None = None
    list_status: str | None = None
    list_date: date | None = None
    index_name: str | None = None
    manager_name: str | None = None
    management_fee: DecimalText | None = None
    management_fee_unit: str | None = None
    capabilities: list[Capability] = Field(default_factory=list)


class InstrumentDetail(DTO):
    instrument: Instrument
    meta: ReadMeta


class InstrumentPage(DTO):
    catalog_key: str
    items: list[Instrument]
    total: int | None = None
    next_cursor: str | None = None
    meta: ReadMeta


class MarketOverview(DTO):
    catalog_key: str
    total_instruments: int | None = None
    listed_count: int | None = None
    exchanges: list[str]
    first_list_date: date | None = None
    latest_list_date: date | None = None
    meta: ReadMeta


class CalendarDay(DTO):
    date: date
    exchange: str
    state: Literal['open', 'closed', 'unknown']
    previous_trading_date: date | None = None
    next_trading_date: date | None = None


class CalendarResult(DTO):
    items: list[CalendarDay]
    meta: ReadMeta


class Units(DTO):
    price: str | None = None
    volume: str | None = None
    amount: str | None = None


class SeriesOption(DTO):
    series_key: str
    label: str
    instrument_key: str
    input_frequency: Literal['daily'] = 'daily'
    periods: list[Period]
    adjustments: list[Adjustment]
    adjustment_capabilities: dict[Adjustment, Capability]
    price_basis: str
    units: Units
    capability: Capability


class MarketBar(DTO):
    bucket_start: date
    bucket_end: date
    first_trade_date: date
    last_trade_date: date
    open: DecimalText | None = None
    high: DecimalText | None = None
    low: DecimalText | None = None
    close: DecimalText | None = None
    volume: DecimalText | None = None
    amount: DecimalText | None = None
    completion: Literal['complete', 'partial', 'unknown']


class Anchor(DTO):
    date: date
    factor: DecimalText


class PriceBasis(DTO):
    adjustment: Adjustment
    policy: str
    as_of: date
    anchor: Anchor | None = None
    note: str


class BarWindow(DTO):
    start: date | None = None
    end: date
    as_of: date


class BarPage(DTO):
    items: list[MarketBar]
    series_key: str
    instrument_key: str
    period: Period
    next_cursor: str | None = None
    units: Units
    window: BarWindow
    basis: PriceBasis
    meta: ReadMeta


class FactorRow(DTO):
    trade_date: date
    factor: DecimalText


class FactorPage(DTO):
    items: list[FactorRow]
    next_cursor: str | None = None
    basis: PriceBasis
    meta: ReadMeta
