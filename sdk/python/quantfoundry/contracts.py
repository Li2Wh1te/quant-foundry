from __future__ import annotations
# Generated from qf-core serde DTO fields; decimal/time wire values are strings.
from typing import Any, Literal, NotRequired, TypedDict

class PositionView(TypedDict):
    security: str
    quantity: int
    sellable: int
    frozen: int
    cost_basis_total: str
    average_cost: str
    market_value: str | None

class AccountView(TypedDict):
    cash: str
    available_cash: str
    frozen_cash: str
    receivables: str
    total_value: str | None
    positions: dict[str, PositionView]

class EquityPoint(TypedDict):
    time_ns: str
    equity: str | None
    unavailable_reason: str | None

class TradingSession(TypedDict):
    key: str
    exchange_timezone: str
    open_ns: str
    close_ns: str

class DataRequest(TypedDict):
    securities: list[str]
    fields: list[str]
    frequency: Literal['1d', '1m', '5m', '15m', '30m', '60m', 'tick']
    start_ns: str | None
    end_ns: str
    count_per_security: int | None
    adjustment: Literal['none', 'pre', 'post']

class DependencyState(TypedDict):
    dataset: str
    generation: str
    readability: str

class DependencyContext(TypedDict):
    scope_id: str
    dependencies: list[DependencyState]

class RunScope(TypedDict):
    run_id: str
    universe: list[str]

class ActualScope(TypedDict):
    start_ns: str | None
    end_ns: str | None
    securities: list[str]

class BatchMetadata(TypedDict):
    schema_id: str
    time_unit: Literal['utc_nanoseconds']
    quantity_unit: Literal['shares']
    price_currency: Literal['CNY']
    rows: int
    actual_scope: ActualScope
    limitations: list[str]

class Fill(TypedDict):
    trade_id: str
    order_id: str
    security: str
    side: Literal['buy', 'sell']
    quantity: int
    price: str
    fee: str
    execution_time_ns: str

class TradePage(TypedDict):
    items: list[Fill]
    next_cursor: str | None

class OrderIntent(TypedDict):
    security: str
    side: Literal['buy', 'sell']
    value: IntentValue
    style: OrderStyle
    tif: Literal['day', 'gtc']
    submitted_at: EventKey

class Order(TypedDict):
    order_id: str
    security: str
    side: Literal['buy', 'sell']
    quantity: int
    filled_quantity: int
    status: Literal['accepted', 'open', 'partially_filled', 'filled', 'cancelled', 'expired', 'rejected']
    submitted_ns: str
    limit_price: str | None
    tif: Literal['day', 'gtc']
    effective_session: str
    eligible_interval_start: str | None
    eligible_after_event: EventKey | None
    reason_code: str | None
    message: str

class OrderResult(TypedDict):
    accepted: bool
    order_id: str | None
    reason_code: str | None
    message: str
    unchanged: bool
    requested_quantity: int | None
    effective_quantity: int | None

class UserRecord(TypedDict):
    time_ns: str
    values: dict[str, RecordValue]

class LogRecord(TypedDict):
    time_ns: str
    level: Literal['info', 'warning', 'error']
    message: str
    truncated: bool

class ResultBatch(TypedDict):
    first_sequence: str
    records: list[ResultRecord]

EffectiveRange = TypedDict('EffectiveRange', {'from': 'str', 'through': 'str'})

class CommissionConfig(TypedDict):
    commission_rate: str
    minimum_commission: str
    currency: Literal['CNY']
    settlement_scale: int
    rounding: Literal['half_even', 'half_up', 'toward_zero', 'away_from_zero']
    included_components: list[Literal['stamp_duty', 'transfer_fee', 'regulatory_fee', 'handling_fee']]
    basis: str

class FeeScope(TypedDict):
    instrument: Instrument
    side: Literal['buy', 'sell']
    investor: Literal['resident_individual', 'resident_enterprise', 'other', 'unknown']
    origin: RuleOrigin

class Instrument(TypedDict):
    security: str
    exchange: Literal['shanghai', 'shenzhen', 'beijing', 'unknown']
    product: Literal['main_board_stock', 'star_stock', 'chi_next_stock', 'beijing_stock', 'equity_etf', 'bond_etf', 'money_etf', 'gold_etf', 'commodity_etf', 'cross_border_etf', 'index', 'unknown']

class CostOverrides(TypedDict):
    commission_rate: NotRequired[str]
    minimum_commission: NotRequired[str]

class DatedFeeComponent(TypedDict):
    kind: Literal['stamp_duty', 'transfer_fee', 'regulatory_fee', 'handling_fee']
    effective_from: str
    effective_through: str
    included_in_commission: bool
    fact: FeeFact

class FeeConfig(TypedDict):
    commission_rate: str
    minimum_commission: str
    currency: Literal['CNY']
    settlement_scale: int
    rounding: Literal['half_even', 'half_up', 'toward_zero', 'away_from_zero']
    components: list[DatedFeeComponent]
    synthetic_model: str | None

class RunConfigFields(TypedDict):
    api_schema: str
    strategy_revision_id: str
    account_id: str
    initial_cash: str
    start: str
    end: str
    frequency: Literal['1d', '1m', '5m', '15m', '30m', '60m', 'tick']
    universe: list[str]
    parameters: dict[str, Any]
    execution_model: Literal['bar_next_interval_v1', 'trade_tick_v1', 'quote_tick_v1']
    benchmark: NotRequired[str | None]
    currency: NotRequired[str]
    reference_calendar: NotRequired[str]
    seed: NotRequired[int]
    participation_rate: NotRequired[str]
    slippage_bps: NotRequired[str]
    risk_free_rate: NotRequired[str]
    annualization_sessions: NotRequired[int]
    result_sampling: NotRequired[Literal['session_close', 'bar_close']]
    cost_overrides: NotRequired[CostOverrides]

class BatchRequest(TypedDict):
    configs: list[RunConfigFields]

class RunSummary(TypedDict):
    run_id: str
    status: Literal['queued', 'starting', 'running', 'succeeded', 'failed', 'cancelled']
    cancel_requested: bool
    progress_completed: int
    progress_total: int | None
    partial: bool
    error: ErrorDTO | None

class PreflightResponse(TypedDict):
    errors: list[ErrorDTO]
    warnings: list[str]
    normalized_config: RunConfigFields | None

class AcceptedRunConfig(TypedDict):
    config: RunConfigFields
    fee_config: FeeConfig
    rule_reference: str

class Capabilities(TypedDict):
    api_schema: str
    contract_version: str
    implemented: list[str]
    execution_models: list[Literal['bar_next_interval_v1', 'trade_tick_v1', 'quote_tick_v1']]
    frequencies: list[Literal['1d', '1m', '5m', '15m', '30m', '60m', 'tick']]
    production_data_available: bool

class ErrorDTO(TypedDict):
    code: str
    operation: str
    message: str
    scope: dict[str, str]

class EventIdentity(TypedDict):
    source_session: str
    channel: str
    sequence: str | None
    stable_input_sequence: str

class MarketUnits(TypedDict):
    price_currency: Literal['CNY']
    quantity_unit: Literal['shares']

class Bar(TypedDict):
    security: str
    session: str
    identity: EventIdentity
    interval_start_ns: str
    interval_end_ns: str
    open: str
    high: str
    low: str
    close: str
    quantity: int | None
    units: MarketUnits

class TradeTick(TypedDict):
    security: str
    session: str
    identity: EventIdentity
    time_ns: str
    price: str
    quantity: int
    units: MarketUnits

class QuoteTick(TypedDict):
    security: str
    session: str
    identity: EventIdentity
    time_ns: str
    bid: str | None
    ask: str | None
    bid_quantity: int | None
    ask_quantity: int | None
    units: MarketUnits

class EventKey(TypedDict):
    time_ns: str
    phase: Literal['settlement', 'market', 'notification', 'callback']
    security: str
    identity: EventIdentity

class OfficialRuleOrigin(TypedDict):
    kind: Literal['official']
    reference: str
class SyntheticRuleOrigin(TypedDict):
    kind: Literal['synthetic']
    reference: str
RuleOrigin = OfficialRuleOrigin | SyntheticRuleOrigin
class MarketStyle(TypedDict):
    kind: Literal['market']
class LimitStyle(TypedDict):
    kind: Literal['limit']
    price: str
OrderStyle = MarketStyle | LimitStyle
class QuantityIntent(TypedDict):
    kind: Literal['quantity', 'target_quantity']
    value: int
class DecimalIntent(TypedDict):
    kind: Literal['value', 'target_value', 'target_percent']
    value: str
IntentValue = QuantityIntent | DecimalIntent
class ApplicableFee(TypedDict):
    applicability: Literal['applicable']
    rate: str
    basis: str
class InapplicableFee(TypedDict):
    applicability: Literal['inapplicable']
    basis: str
FeeFact = ApplicableFee | InapplicableFee
class BarEvent(TypedDict):
    kind: Literal['bar']
    event: Bar
class TradeEvent(TypedDict):
    kind: Literal['trade_tick']
    event: TradeTick
class QuoteEvent(TypedDict):
    kind: Literal['quote_tick']
    event: QuoteTick
MarketEvent = BarEvent | TradeEvent | QuoteEvent
class RunSucceeded(TypedDict):
    status: Literal['succeeded']
    result_id: str
class RunFailed(TypedDict):
    status: Literal['failed']
    error: ErrorDTO
    partial: bool
class RunCancelled(TypedDict):
    status: Literal['cancelled']
    partial: bool
RunOutcome = RunSucceeded | RunFailed | RunCancelled
class DecimalRecord(TypedDict):
    kind: Literal['decimal']
    value: str
class IntegerRecord(TypedDict):
    kind: Literal['integer']
    value: int
class TextRecord(TypedDict):
    kind: Literal['text']
    value: str
class StatisticRecord(TypedDict):
    kind: Literal['statistic']
    value: float
RecordValue = DecimalRecord | IntegerRecord | TextRecord | StatisticRecord
class EquityRecord(TypedDict):
    kind: Literal['equity']
    record: EquityPoint
class OrderRecord(TypedDict):
    kind: Literal['order']
    record: Order
class TradeRecord(TypedDict):
    kind: Literal['trade']
    record: Fill
class PositionRecord(TypedDict):
    kind: Literal['position']
    record: PositionView
class UserRecordEnvelope(TypedDict):
    kind: Literal['record']
    record: UserRecord
class LogRecordEnvelope(TypedDict):
    kind: Literal['log']
    record: LogRecord
ResultRecord = EquityRecord | OrderRecord | TradeRecord | PositionRecord | UserRecordEnvelope | LogRecordEnvelope
