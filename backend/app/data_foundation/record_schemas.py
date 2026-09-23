"""Executable canonical schemas, not an unrestricted native-JSON publication.

Every admitted field has a type and an observation-level meaning. Unknown
quantitative units are represented by an unavailable optional field, never by
copying a number into a guessed monetary or percentage unit. New domains must
install an explicit schema/adapter before they can publish any canonical body.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator

from app.data_foundation.canonical import FoundationError


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid')


class SourceLocalTableFact(Body):
    native_dataset: StrictStr = Field(min_length=1, max_length=80)
    native_table: StrictStr = Field(min_length=1, max_length=80)
    source_identity: StrictStr = Field(pattern=r'^[0-9a-f]{64}$')
    source_row_hash: StrictStr = Field(pattern=r'^[0-9a-f]{64}$')
    reported_text: dict[StrictStr, StrictStr | None]
    reported_numbers: dict[StrictStr, StrictStr | None]
    reported_dates: dict[StrictStr, date | None]
    reported_times: dict[StrictStr, AwareDatetime | None]
    reported_identifiers: dict[StrictStr, StrictStr | None]
    reported_flags: dict[StrictStr, StrictBool | None]
    reported_nested_hashes: dict[StrictStr, StrictStr | None]
    nested_semantics: Literal['source_hash_only']
    verified_public_time: None = None
    verified_market_completeness: None = None


class EmptyLocalScope(Body):
    provider: Literal['tonghuashun', 'tushare']
    native_dataset: StrictStr = Field(min_length=1, max_length=80)
    scope_identity: StrictStr = Field(min_length=1, max_length=128)
    capture_identity: StrictStr = Field(min_length=1, max_length=128)
    fixed_rows: Literal[0]
    evidence_basis: Literal['sealed_empty_scan', 'fixed_empty_table']
    captured_at: AwareDatetime
    semantics: Literal['no_rows_in_fixed_local_capture']
    market_absence: None = None
    market_coverage_complete: None = None


class ImportArtifact(Body):
    dump_id: StrictStr = Field(min_length=1)
    sha256: StrictStr = Field(pattern=r'^[0-9a-f]{64}$')
    request_id: StrictStr = Field(min_length=1)
    columns: list[StrictStr] = Field(min_length=1)
    rows: StrictInt = Field(ge=0)
    download_bytes: StrictInt = Field(ge=0)
    observed_start: date
    observed_end: date
    numeric_encoding: StrictStr = Field(min_length=1)
    collected_at: AwareDatetime | None = None
    future_event_rows: StrictInt | None = Field(default=None, ge=0)
    negative_bonus_rows: StrictInt | None = Field(default=None, ge=0)


class ImportPending(Body):
    reason: Literal['batch_budget']
    pending: StrictInt = Field(gt=0)


class ImportProgress(Body):
    import_channel: Literal['stock_daily_dump', 'stock_recent_dump', 'stock_actions_dump']
    source_scope: Literal['market']
    artifact: ImportArtifact
    pending_requests: list[ImportPending] = Field(max_length=1)
    imported_subjects: StrictInt = Field(ge=0)
    superseded_subjects: StrictInt = Field(ge=0)
    total_subjects: StrictInt = Field(ge=0)
    requested_start: date
    requested_end: date
    semantics: Literal['local_import_progress_only']
    business_publication_complete: None = None

    @model_validator(mode='after')
    def consistent_progress(self):
        # Import progress is authoritative only within this exact downloaded
        # artifact. It never certifies market coverage or formal publication.
        if not self.superseded_subjects <= self.imported_subjects <= self.total_subjects:
            raise ValueError('导入计数超出固定文件分母')
        pending = self.total_subjects - self.imported_subjects
        if pending != sum(item.pending for item in self.pending_requests):
            raise ValueError('待导入计数与固定文件分母不一致')
        if (self.requested_start > self.requested_end
                or self.requested_start != self.artifact.observed_start
                or self.requested_end != self.artifact.observed_end):
            raise ValueError('导入日期与固定文件范围不一致')
        return self


class InstrumentReference(Body):
    source_code: StrictStr = Field(min_length=1, max_length=64)
    asset_type: StrictStr = Field(min_length=1, max_length=32)
    name: StrictStr | None = None
    exchange: StrictStr | None = None
    currency: StrictStr | None = None
    listing_date: date | None = None
    end_date: date | None = None


class FundCompany(Body):
    company_id: StrictStr = Field(min_length=1, max_length=128)
    name: StrictStr | None = None
    company_type: StrictStr | None = None
    established_date: date | None = None
    fund_count: StrictInt | None = Field(default=None, ge=0)


class FundManager(Body):
    manager_id: StrictStr = Field(min_length=1, max_length=128)
    name: StrictStr | None = None
    company_id: StrictStr | None = None
    company_name: StrictStr | None = None
    degree: StrictStr | None = None
    resume: StrictStr | None = None


class FundProfile(Body):
    source_code: StrictStr = Field(min_length=1, max_length=64)
    name: StrictStr | None = None
    company_id: StrictStr | None = None
    manager_name: StrictStr | None = None
    management_name: StrictStr | None = None
    established_date: date | None = None


class ManagerAssignment(Body):
    """Source-reported career text, without inferred economic dates or IDs."""
    fund_code: StrictStr = Field(min_length=1, max_length=64)
    name: StrictStr | None = None
    quotation_code: StrictStr | None = None
    fund_type: StrictStr | None = None
    start_text: StrictStr | None = None
    end_text: StrictStr | None = None


class ManagerExperience(Body):
    """A complete reported assignment set replaces its previous observation.

    Opaque provider periods (including '至今') remain text. Performance, awards
    and heavy-asset facts require separate verified definitions; declaring them
    unavailable prevents consumers from treating omitted facts as zero.
    """
    manager_id: StrictStr = Field(min_length=1, max_length=128)
    assignments: list[ManagerAssignment]
    performance: None = None
    awards: None = None
    heavy_assets: None = None

    @field_validator('assignments')
    @classmethod
    def unique_sorted_assignments(cls, value):
        keys = [row.fund_code for row in value]
        if len(keys) != len(set(keys)) or keys != sorted(keys):
            raise ValueError('Assignments must have unique, sorted source codes')
        return value


ReportedNav = Annotated[StrictStr, Field(pattern=r'^(0|[1-9][0-9]{0,31})(\.[0-9]{1,32})?$')]


class NavPoint(Body):
    """Exact provider categories, not a currency/share or total-return claim."""
    nav_date: date
    reported_unit_nav: ReportedNav | None
    reported_adjusted_nav: ReportedNav | None

    @model_validator(mode='after')
    def has_reported_value(self):
        if self.reported_unit_nav is None and self.reported_adjusted_nav is None:
            raise ValueError('A NAV point must report at least one category')
        return self


class FundNavSnapshot(Body):
    """One complete materialized sequence with an observation-specific basis.

    The collector replaces reconciled rolling windows, so retaining absent
    dates from the previous official snapshot would mix adjustment bases.
    Reported categories remain separate and cannot authorize return, currency
    conversion or cumulative-NAV calculations. Dates describe NAV business
    dates, never historical availability to market participants.
    """
    source_code: StrictStr = Field(min_length=1, max_length=64)
    coverage: Literal['provider_rolling_window']
    value_basis: Literal['tonghuashun_nav_type_unit_adj']
    sequence_start: date
    sequence_end: date
    points: list[NavPoint] = Field(min_length=1)
    currency: None = None
    share_basis: None = None
    adjustment_formula: None = None
    cumulative_nav: None = None

    @model_validator(mode='after')
    def coherent_sequence(self):
        days = [point.nav_date for point in self.points]
        if days != sorted(set(days)):
            raise ValueError('NAV dates must be unique and sorted')
        if self.sequence_start != days[0] or self.sequence_end != days[-1]:
            raise ValueError('Sequence coverage must match materialized points')
        return self


class OfferingMember(Body):
    source_code: StrictStr = Field(min_length=1, max_length=64)
    ticker: StrictStr | None = None
    reported_subscription_start: AwareDatetime | None = None
    reported_subscription_end: AwareDatetime | None = None

    @field_validator('reported_subscription_start', 'reported_subscription_end', mode='before')
    @classmethod
    def explicit_timestamp(cls, value):
        if value is not None and not isinstance(value, (str, datetime)):
            raise ValueError('Canonical offering timestamps must be explicit UTC instants')
        return value

    @field_validator('reported_subscription_start', 'reported_subscription_end')
    @classmethod
    def utc_milliseconds(cls, value):
        if value is not None and (value.utcoffset() != timedelta(0) or value.microsecond % 1000):
            raise ValueError('Offering timestamp must preserve UTC milliseconds')
        return value

    @model_validator(mode='after')
    def ordered_window(self):
        if self.reported_subscription_start is not None and self.reported_subscription_end is not None:
            if self.reported_subscription_start > self.reported_subscription_end:
                raise ValueError('Reported subscription window is reversed')
        return self


class FundOfferingSnapshot(Body):
    """A source-filtered offering list, without asserted live eligibility.

    Source window boundaries can include intraday closing times. Keep their
    UTC instants instead of silently truncating them into calendar dates. The
    reported active/upcoming filter applies to this observation only.
    """
    collection_key: Literal['active', 'upcoming']
    selection_basis: Literal['provider_subscription_filter']
    members: list[OfferingMember]
    current_subscription_eligibility: None = None

    @field_validator('members')
    @classmethod
    def unique_sorted_members(cls, value):
        keys = [member.source_code for member in value]
        if keys != sorted(set(keys)):
            raise ValueError('Offering members must have unique sorted source codes')
        return value


class CalendarDay(Body):
    exchange_scope: StrictStr = Field(min_length=1, max_length=128)
    calendar_date: date
    is_open: StrictBool | None
    scope_basis: Literal['declared_exchange', 'provider_reported_dates']


class IndexMember(Body):
    source_code: StrictStr = Field(min_length=1, max_length=64)
    name: StrictStr | None = None
    ticker: StrictStr | None = None


class IndexCollection(Body):
    """One indivisible provider-reported set, without inferred effective dates.

    Replacing the collection replaces all its members. An empty list is an
    explicit empty observation; omitted or malformed lists cannot become one.
    Local source identifiers do not assert cross-provider instrument bindings.
    """
    collection_key: StrictStr = Field(min_length=1, max_length=64)
    collection_kind: Literal['category', 'index']
    membership_basis: Literal['provider_reported_snapshot']
    members: list[IndexMember]

    @field_validator('members')
    @classmethod
    def unique_sorted_members(cls, value):
        keys = [member.source_code for member in value]
        if len(keys) != len(set(keys)):
            raise ValueError('Duplicate collection member identity')
        if keys != sorted(keys):
            raise ValueError('Collection members must be canonically sorted')
        return value


class IndexCategorySnapshot(IndexCollection):
    collection_key: Literal['cn_concept', 'region', 'tszs', 'industry']
    collection_kind: Literal['category']


class IndexConstituentSnapshot(IndexCollection):
    collection_kind: Literal['index']


class AdjustmentFactor(Body):
    """Provider factors are retained without inventing a price-adjustment anchor.

    Supplier field basis: https://tushare.pro/document/2?doc_id=199 (reviewed
    2026-09-22). The local persisted NUMERIC(24,12) value is serialized exactly;
    this contract does not derive adjusted prices or assert public-time history.
    """
    source_code: StrictStr = Field(min_length=1, max_length=64)
    trade_date: date
    factor: StrictStr = Field(pattern=r'^(0|[1-9][0-9]{0,11})(\.[0-9]{1,12})?$')
    factor_basis: Literal['tushare_fund_adj']
    anchor_date: date | None = None

    @field_validator('factor')
    @classmethod
    def positive_factor(cls, value):
        if Decimal(value) <= 0:
            raise ValueError('Adjustment factor must be positive')
        return value


DailyPrice = Annotated[StrictStr, Field(pattern=r'^(0|[1-9][0-9]{0,13})(\.[0-9]{1,6})?$')]
DailyQuantity = Annotated[StrictStr, Field(pattern=r'^(0|[1-9][0-9]{0,19})(\.[0-9]{1,4})?$')]
DailyChange = Annotated[StrictStr, Field(pattern=r'^-?(0|[1-9][0-9]{0,13})(\.[0-9]{1,6})?$')]


class FundDailyObservation(Body):
    """Typed fund_daily facts in documented native units and stored precision.

    https://tushare.pro/document/2?doc_id=127 establishes yuan prices, lots,
    thousand-yuan amounts and percentage changes. Keep these units explicit;
    neither share quantities nor adjusted prices are derived here. A source
    code identifies the observation, not a verified historical economic entity.
    """
    source_code: StrictStr = Field(min_length=1, max_length=64)
    trade_date: date
    open: DailyPrice
    high: DailyPrice
    low: DailyPrice
    close: DailyPrice
    price_unit: Literal['yuan']
    price_basis: Literal['provider_reported']
    volume_lots: DailyQuantity | None = None
    turnover_thousand_yuan: DailyQuantity | None = None
    previous_close: DailyPrice | None = None
    change_yuan: DailyChange | None = None
    change_percent: DailyChange | None = None

    @model_validator(mode='after')
    def coherent_prices(self):
        opened, high, low, close = (Decimal(getattr(self, key)) for key in ('open', 'high', 'low', 'close'))
        if low <= 0 or high < max(opened, close) or low > min(opened, close):
            raise ValueError('Daily OHLC values must be positive and coherent')
        if self.previous_close is not None and Decimal(self.previous_close) <= 0:
            raise ValueError('Previous close must be positive when available')
        return self


class PopularityMember(Body):
    """Provider ordering and heat remain source-reported observations only."""
    source_code: StrictStr = Field(min_length=1, max_length=64)
    name: StrictStr | None = None
    ticker: StrictStr | None = None
    reported_rank: StrictInt = Field(ge=1)
    reported_heat: ReportedNav | None = None
    reported_rank_change: StrictInt | None = None
    reported_rank_trend: StrictStr | None = None


class PopularitySnapshot(Body):
    collection_key: StrictStr = Field(min_length=1, max_length=128)
    period: Literal['day', 'hour']
    scope_basis: Literal['provider_returned_ranking_list']
    members: list[PopularityMember]
    heat_method: None = None
    historical_public_time: None = None

    @field_validator('members')
    @classmethod
    def unique_ordered_members(cls, members):
        codes = [m.source_code for m in members]
        order = [(m.reported_rank, m.source_code) for m in members]
        if len(codes) != len(set(codes)) or order != sorted(order):
            raise ValueError('Ranked members must have unique codes and deterministic rank order')
        return members


class HistoricalPopularitySnapshot(PopularitySnapshot):
    period: Literal['historical_day']
    ranking_date: date

    @model_validator(mode='after')
    def date_matches_collection(self):
        if self.collection_key != self.ranking_date.isoformat():
            raise ValueError('Historical list identity must match its business date')
        return self


class QuotaSummaryGroup(Body):
    category_key: StrictStr = Field(min_length=1, max_length=128)
    reported_buy_text: StrictStr
    reported_total_text: StrictStr
    reported_total_limit_text: StrictStr
    reported_unlimited_text: StrictStr


class QuotaFund(Body):
    source_code: StrictStr = Field(min_length=1, max_length=64)
    fund_name: StrictStr
    reported_quota_text: StrictStr
    reported_year_text: StrictStr
    classify_presence: Literal['absent', 'null', 'value']
    reported_classify: list[StrictStr] | None

    @model_validator(mode='after')
    def distinguish_missing_classification(self):
        if (self.classify_presence == 'value') != (self.reported_classify is not None):
            raise ValueError('Classification presence and value must agree')
        return self


class QuotaSubcategory(Body):
    category_key: StrictStr = Field(min_length=1, max_length=128)
    funds: list[QuotaFund]

    @field_validator('funds')
    @classmethod
    def distinct_sorted_codes(cls, members):
        codes = [m.source_code for m in members]
        if len(codes) != len(set(codes)) or codes != sorted(codes):
            raise ValueError('A quota subcategory must have distinct sorted codes')
        return members


class QuotaListGroup(Body):
    category_key: StrictStr = Field(min_length=1, max_length=128)
    subcategories: list[QuotaSubcategory]

    @field_validator('subcategories')
    @classmethod
    def distinct_sorted_categories(cls, groups):
        keys = [g.category_key for g in groups]
        if len(keys) != len(set(keys)) or keys != sorted(keys):
            raise ValueError('Quota subcategories must be distinct and sorted')
        return groups


class QuotaSummarySnapshot(Body):
    collection_key: StrictStr = Field(min_length=1, max_length=128)
    scope_basis: Literal['explicit_requested_category_only']
    reported_groups: list[QuotaSummaryGroup] = Field(max_length=1)
    comparable_quota_amounts: None = None
    quota_currency: None = None
    current_subscription_eligibility: None = None

    @model_validator(mode='after')
    def group_matches_requested_category(self):
        if any(g.category_key != self.collection_key for g in self.reported_groups):
            raise ValueError('Reported quota category must match the requested category')
        return self


class QuotaListSnapshot(QuotaSummarySnapshot):
    reported_groups: list[QuotaListGroup] = Field(max_length=1)
    annual_return: None = None


class ReportedPricePoint(Body):
    trade_date: date
    reported_open: ReportedNav
    reported_high: ReportedNav
    reported_low: ReportedNav
    reported_close: ReportedNav
    reported_volume: ReportedNav | None
    reported_turnover: ReportedNav | None

    @model_validator(mode='after')
    def coherent_reported_prices(self):
        opened, high, low, close = (Decimal(getattr(self, 'reported_' + key)) for key in ('open', 'high', 'low', 'close'))
        if low <= 0 or high < max(opened, close) or low > min(opened, close):
            raise ValueError('Reported OHLC prices must be positive and coherent')
        return self


class DailyPriceWindow(Body):
    """Complete materialized observation, not complete requested-date coverage."""
    source_code: StrictStr = Field(min_length=1, max_length=64)
    coverage: Literal['observed_rows_only']
    sequence_start: date
    sequence_end: date
    points: list[ReportedPricePoint] = Field(min_length=1)
    declared_request_start: date | None
    declared_request_end: date | None
    currency: None = None
    volume_unit: None = None
    turnover_unit: None = None
    adjustment_anchor: None = None
    adjustment_formula: None = None

    @model_validator(mode='after')
    def coherent_observed_window(self):
        days = [point.trade_date for point in self.points]
        if days != sorted(set(days)) or self.sequence_start != days[0] or self.sequence_end != days[-1]:
            raise ValueError('Daily window bounds must match distinct sorted materialized dates')
        if self.declared_request_start and self.declared_request_end and self.declared_request_start > self.declared_request_end:
            raise ValueError('Reported request dates are reversed')
        return self


class StockDailyWindow(DailyPriceWindow):
    asset_type: Literal['a-share']
    reported_adjustment: Literal['none']


class EtfDailyWindow(DailyPriceWindow):
    asset_type: Literal['fund-etf']
    reported_adjustment: Literal['forward']


class IndexDailyWindow(DailyPriceWindow):
    asset_type: Literal['a-share-index']
    reported_adjustment: Literal['not_applicable']


ReportedSigned = Annotated[StrictStr, Field(pattern=r'^-?(0|[1-9][0-9]{0,31})(\.[0-9]{1,32})?$')]


class QuoteSnapshot(Body):
    reported_value_status: Literal['values_reported', 'explicit_null_report']
    reported_ticker: StrictStr | None
    reported_name: StrictStr | None
    source_code: StrictStr = Field(min_length=1, max_length=64)
    reported_at: AwareDatetime | None
    timestamp_semantics: None = None
    effective_business_date: None = None
    reported_open_price: ReportedNav | None
    reported_high_price: ReportedNav | None
    reported_low_price: ReportedNav | None
    reported_last_price: ReportedNav | None
    reported_prev_price: ReportedNav | None
    reported_price_change: ReportedSigned | None
    reported_price_change_ratio_pct: ReportedSigned | None
    reported_price_amplitude_ratio_pct: ReportedNav | None
    reported_turnover_ratio_pct: ReportedNav | None
    reported_volume: ReportedNav | None
    reported_turnover: ReportedNav | None
    currency: None = None
    volume_unit: None = None
    turnover_unit: None = None
    executable_quote: None = None
    trading_status: None = None


    @model_validator(mode='after')
    def value_status_matches_reported_quantities(self):
        ignored = {'reported_at', 'reported_ticker', 'reported_name', 'reported_value_status'}
        any_value = any(getattr(self, key) is not None for key in type(self).model_fields
                        if key.startswith('reported_') and key not in ignored)
        expected = 'values_reported' if any_value else 'explicit_null_report'
        if self.reported_value_status != expected:
            raise ValueError('Reported value status disagrees with typed quantities')
        return self


class StockQuoteSnapshot(QuoteSnapshot):
    asset_type: Literal['a-share']


class EtfQuoteSnapshot(QuoteSnapshot):
    asset_type: Literal['fund-etf']


class IndexQuoteSnapshot(QuoteSnapshot):
    asset_type: Literal['a-share-index']


class StockValuationSnapshot(Body):
    reported_value_status: Literal['values_reported', 'explicit_null_report']
    reported_ticker: StrictStr | None
    reported_name: StrictStr | None
    source_code: StrictStr = Field(min_length=1, max_length=64)
    asset_type: Literal['a-share']
    reported_at: AwareDatetime | None
    timestamp_semantics: None = None
    effective_business_date: None = None
    reported_pe_ttm: ReportedSigned | None
    reported_pe_mrq: ReportedSigned | None
    reported_pb_mrq: ReportedSigned | None
    reported_ps_ttm: ReportedSigned | None
    reported_pcf_ttm: ReportedSigned | None
    valuation_formula: None = None
    financial_period: None = None


    @model_validator(mode='after')
    def value_status_matches_reported_quantities(self):
        ignored = {'reported_at', 'reported_ticker', 'reported_name', 'reported_value_status'}
        any_value = any(getattr(self, key) is not None for key in type(self).model_fields
                        if key.startswith('reported_') and key not in ignored)
        expected = 'values_reported' if any_value else 'explicit_null_report'
        if self.reported_value_status != expected:
            raise ValueError('Reported value status disagrees with typed quantities')
        return self


class ActivityMember(Body):
    source_code: StrictStr = Field(min_length=1, max_length=64)
    reported_ticker: StrictStr | None = None
    reported_name: StrictStr | None = None


class AuctionSnapshot(ActivityMember):
    asset_type: Literal['a-share']
    reported_phase: Literal['closed']
    reported_status: Literal['final']
    reported_at: AwareDatetime | None
    reported_auction_amount: ReportedNav | None
    reported_auction_pct: ReportedSigned | None
    reported_auction_price: ReportedNav | None
    reported_auction_turnover_pct: ReportedNav | None
    reported_auction_unmatched: ReportedSigned | None
    reported_auction_volume: ReportedNav | None
    reported_auction_volume_ratio: ReportedNav | None
    reported_auction_yesterday_ratio_pct: ReportedNav | None
    reported_float_market_cap: ReportedNav | None
    reported_last_price: ReportedNav | None
    reported_open_price: ReportedNav | None
    reported_pre_close_price: ReportedNav | None
    currency: None = None
    quantity_units: None = None
    auction_formula: None = None
    executable_quote: None = None
    effective_business_date: None = None


class AuctionBenchmarkMember(ActivityMember):
    reported_auction_pct: ReportedSigned | None
    reported_tags: list[StrictStr]


class DatedActivitySet(Body):
    collection_key: StrictStr = Field(min_length=1, max_length=128)
    trading_date: date
    scope_basis: Literal['source_reported_list_only']
    complete_market_coverage: None = None


class AuctionBenchmarkSnapshot(DatedActivitySet):
    members: list[AuctionBenchmarkMember]
    selection_formula: None = None


class LimitUpMember(ActivityMember):
    reported_continue_day_cnt: StrictInt | None = Field(ge=0)
    reported_continue_day_text: StrictStr | None
    reported_is_new: StrictBool | None
    reported_is_st: StrictBool | None
    reported_last_price: ReportedNav | None
    reported_limit_up_reason: StrictStr | None
    reported_limit_up_time: StrictStr | None
    reported_max_seal_money: ReportedNav | None
    reported_price_change_ratio_pct: ReportedSigned | None
    reported_seal_money: ReportedNav | None


class LimitDownMember(ActivityMember):
    reported_first_limit_time: StrictStr | None
    reported_last_limit_time: StrictStr | None
    reported_last_price: ReportedNav | None
    reported_price_change_ratio_pct: ReportedSigned | None
    reported_turnover_ratio_pct: ReportedNav | None


class LimitBreakMember(ActivityMember):
    reported_last_price: ReportedNav | None
    reported_open_times: StrictInt | None = Field(ge=0)
    reported_price_change_ratio_pct: ReportedSigned | None
    reported_turnover: ReportedNav | None
    reported_turnover_ratio_pct: ReportedNav | None


class LimitSet(DatedActivitySet):
    currency: None = None
    amount_units: None = None
    exchange_limit_rule: None = None
    event_timestamps: None = None


class LimitUpSnapshot(LimitSet):
    members: list[LimitUpMember]


class LimitDownSnapshot(LimitSet):
    members: list[LimitDownMember]


class LimitBreakSnapshot(LimitSet):
    members: list[LimitBreakMember]


class AnomalyMember(Body):
    source_order: StrictInt = Field(ge=0)
    source_code: StrictStr = Field(min_length=1, max_length=64)
    reported_name: StrictStr | None
    reported_tag: StrictStr | None
    reported_keywords: list[StrictStr]
    reported_analysis: StrictStr | None


class AnomalySnapshot(Body):
    collection_key: StrictStr = Field(min_length=1, max_length=128)
    members: list[AnomalyMember]
    scope_basis: Literal['source_reported_narratives_only']
    factual_verification: None = None
    event_timestamps: None = None
    complete_market_coverage: None = None


class LadderMember(ActivityMember):
    reported_board_num: StrictInt = Field(ge=1)
    reported_seal_nextday: StrictBool | None
    reported_sign_level: StrictInt | None


class LadderGroups(Body):
    two_board: list[LadderMember]
    three_board: list[LadderMember]
    four_board: list[LadderMember]
    five_board: list[LadderMember]
    six_board: list[LadderMember]
    seven_over: list[LadderMember]


class LadderCaps(Body):
    two_board: StrictInt = Field(ge=0)
    three_board: StrictInt = Field(ge=0)
    four_board: StrictInt = Field(ge=0)
    five_board: StrictInt = Field(ge=0)
    six_board: StrictInt = Field(ge=0)
    seven_over: StrictInt = Field(ge=0)


class LadderDay(Body):
    trading_date: date
    reported_groups: LadderGroups


class LimitLadderWindow(Body):
    collection_key: StrictStr = Field(min_length=1, max_length=128)
    coverage: Literal['provider_rolling_window']
    days: list[LadderDay]
    declared_board_caps: LadderCaps
    complete_market_coverage: None = None
    seal_nextday_meaning: None = None
    sign_level_meaning: None = None

    @model_validator(mode='after')
    def distinct_chronological_days(self):
        days = [day.trading_date for day in self.days]
        if days != sorted(set(days)):
            raise ValueError('Ladder dates must be distinct and chronological')
        return self


class DragonTigerStock(ActivityMember):
    source_order: StrictInt = Field(ge=0)
    reported_range_days: StrictInt = Field(ge=1)
    reported_limit_reason: StrictStr | None
    reported_concepts: list[StrictStr]
    reported_change: ReportedSigned | None
    reported_net_rate: ReportedSigned | None
    reported_net_value: ReportedSigned | None
    reported_org_net_rate: ReportedSigned | None
    reported_org_net_value: ReportedSigned | None
    reported_hot_money_net_rate: ReportedSigned | None
    reported_hot_money_net_value: ReportedSigned | None
    reported_hot_money_item_net_rate: ReportedSigned | None
    reported_hot_money_item_net_value: ReportedSigned | None
    reported_amount: ReportedNav | None
    reported_buy_value: ReportedNav | None
    reported_sell_value: ReportedNav | None
    reported_hot_rank: StrictInt | None = Field(ge=0)
    reported_org_buy_num: StrictInt | None = Field(ge=0)
    reported_org_sell_num: StrictInt | None = Field(ge=0)


class DragonTigerParticipant(Body):
    source_order: StrictInt = Field(ge=0)
    reported_name: StrictStr
    reported_buying: ReportedSigned | None
    stocks: list[DragonTigerStock]


class DragonTigerSnapshot(Body):
    collection_key: StrictStr
    trading_date: date
    board_type: Literal['all','org','hot_money']
    stocks: list[DragonTigerStock]
    hot_money: list[DragonTigerParticipant]
    reported_count: StrictInt | None = Field(ge=0)
    reported_stock_count: StrictInt | None = Field(ge=0)
    complete_market_coverage: None = None
    currency: None = None
    amount_units: None = None
    ratio_formula: None = None
    participant_identity: None = None
    duplicate_resolution: None = None
    period_start: None = None


class PerformanceBase(Body):
    source_code: StrictStr
    asset_type: Literal['fund']
    calculation_formula: None = None
    comparable_units: None = None
    effective_period_boundaries: None = None
    complete_history: None = None


class PerformancePeriod(Body):
    provider_period: Literal['week','month','tmonth','hyear','year','twoyear','tyear','fyear','now','nowyear']
    reported_fields: list[StrictStr]


class ReturnPeriod(PerformancePeriod):
    reported_return: ReportedSigned | None
    reported_peer_average: ReportedSigned | None
    reported_rank: StrictInt | None = Field(ge=0)
    reported_rank_total: StrictInt | None = Field(ge=0)


class DrawdownPeriod(PerformancePeriod):
    reported_drawdown: ReportedSigned | None


class PerformanceSnapshot(PerformanceBase):
    reported_ticker: StrictStr | None
    reported_timestamp_ms: StrictInt | None = Field(ge=0)
    timestamp_semantics: None = None


class ReturnSnapshot(PerformanceSnapshot):
    periods: list[ReturnPeriod]


class DrawdownSnapshot(PerformanceSnapshot):
    periods: list[DrawdownPeriod]


class PerformancePoint(Body):
    trading_date: date
    reported_fields: list[StrictStr]
    reported_donchian_channel: ReportedSigned | None
    reported_rsi_pct: ReportedSigned | None
    reported_track_index_pe_ttm_five_year_percentile: ReportedSigned | None


class PerformanceWindow(PerformanceBase):
    coverage: Literal['observed_rows_only']
    points: list[PerformancePoint]


class ManagerStylePeriod(Body):
    source_order: StrictInt = Field(ge=0)
    reported_period_tag: StrictStr
    reported_vector: list[ReportedNav | None] = Field(min_length=7,max_length=7)
    reported_total_fund_scale: ReportedNav | None


class ManagerStyleSnapshot(Body):
    manager_id: StrictStr
    preferences: list[ManagerStylePeriod]
    reported_timestamp_ms: StrictInt | None = Field(ge=0)
    reported_investment_idea: StrictStr | None
    reported_representative_fund_code: StrictStr | None
    reported_representative_fund_ticker: StrictStr | None
    reported_representative_fund_name: StrictStr | None
    reported_total_fund_scale: ReportedNav | None
    industry_taxonomy: None = None
    normalized_industry_allocation: None = None
    scale_currency: None = None
    scale_unit: None = None
    period_boundaries: None = None
    timestamp_semantics: None = None
    verified_investment_strategy: None = None


class ManagerPerformancePoint(Body):
    trading_date: date
    reported_fields: list[StrictStr]
    reported_manager_return_pct: ReportedSigned | None
    reported_peer_return_pct: ReportedSigned | None
    reported_benchmark_return_pct: ReportedSigned | None


class ManagerPerformanceWindow(Body):
    manager_id: StrictStr
    provider_period: Literal['month','tmonth','year','nowyear','now']
    points: list[ManagerPerformancePoint]
    reported_timestamp_ms: StrictInt | None = Field(ge=0)
    coverage_basis: Literal['source_observation_window']
    calculation_formula: None = None
    comparable_units: None = None
    benchmark_identity: None = None
    excess_return: None = None
    effective_period_boundaries: None = None
    complete_history: None = None
    timestamp_semantics: None = None


class DistributionWindow(Body):
    source_code: StrictStr
    asset_type: Literal['fund', 'a-share']
    coverage_basis: Literal['source_observation_window']
    payment_execution: None = None
    normalized_cashflow: None = None
    event_identity: None = None
    complete_history: None = None


class FundDividendEvent(Body):
    source_order: StrictInt = Field(ge=0)
    reported_fields: list[StrictStr]
    reported_ex_dividend_date: date | None
    reported_in_dividend_date: date | None
    reported_payment_date: date | None
    reported_profit_base_date: date | None
    reported_publish_date: date | None
    reported_registration_date: date | None
    reported_reinvestment_date: date | None
    reported_per_ten_cash_after_tax: ReportedSigned | None
    reported_per_ten_cash_before_tax: ReportedSigned | None
    reported_progress_code: StrictStr | None


class FundDividendWindow(DistributionWindow):
    asset_type: Literal['fund']
    events: list[FundDividendEvent]
    reported_dividend_count: StrictInt | None = Field(ge=0)
    reported_dividend_total: ReportedSigned | None
    reported_timestamp_ms: StrictInt | None = Field(ge=0)
    currency: None = None
    tax_basis: None = None


class CorporateActionEvent(Body):
    source_order: StrictInt = Field(ge=0)
    reported_fields: list[StrictStr]
    reported_ex_date: date | None
    reported_dividend_per_share: ReportedSigned | None
    reported_per_share_bonus: ReportedSigned | None
    reported_allotment_ratio: ReportedSigned | None
    reported_allotment_price: ReportedSigned | None
    reported_ticker: StrictStr | None
    reported_currency: StrictStr | None


class CorporateActionWindow(DistributionWindow):
    asset_type: Literal['a-share']
    events: list[CorporateActionEvent]
    reported_ticker: StrictStr | None
    reported_adjustment: StrictStr | None
    requested_start: date | None
    requested_end: date | None
    reported_coverage: StrictStr | None


class CompositionMember(Body):
    source_order: StrictInt = Field(ge=0)
    reported_fields: list[StrictStr]


class FundAllocationMember(CompositionMember):
    reported_bond_ratio_pct: ReportedSigned | None
    reported_deposit_ratio_pct: ReportedSigned | None
    reported_other_ratio_pct: ReportedSigned | None
    reported_stock_ratio_pct: ReportedSigned | None
    reported_report_date: date | None


class FundIndustryMember(CompositionMember):
    reported_ratio_pct: ReportedSigned | None
    reported_industry_name: StrictStr | None
    reported_report_period: StrictStr | None


class FundHolderMember(CompositionMember):
    reported_avg_holder_share: ReportedSigned | None
    reported_ins_position: ReportedSigned | None
    reported_mgmt_staff_hold_rate: ReportedSigned | None
    reported_psnl_rate: ReportedSigned | None
    reported_merge_scope: StrictStr | None
    reported_report_date: date | None
    reported_holder_amount: StrictInt | None = Field(ge=0)


class FundTopHolderMember(CompositionMember):
    reported_hold_rate_pct: ReportedSigned | None
    reported_hold_share: ReportedSigned | None
    reported_holder_code: StrictStr | None
    reported_holder_id: StrictStr | None
    reported_holder_name: StrictStr | None
    reported_holder_type: StrictStr | None
    reported_publish_date: date | None
    reported_report_date: date | None
    reported_rank: StrictInt | None = Field(ge=0)


class FundCompositionWindow(Body):
    source_code: StrictStr
    asset_type: Literal['fund']
    reported_timestamp_ms: StrictInt | None = Field(ge=0)
    reported_limit: StrictInt | None = Field(ge=0)
    coverage_basis: Literal['source_observation_window']
    comparable_units: None = None
    resolved_holder_identity: None = None
    normalized_allocation: None = None
    industry_taxonomy: None = None
    complete_history: None = None


class FundAllocationWindow(FundCompositionWindow):
    members: list[FundAllocationMember]


class FundIndustryWindow(FundCompositionWindow):
    members: list[FundIndustryMember]


class FundHolderWindow(FundCompositionWindow):
    members: list[FundHolderMember]


class FundTopHolderWindow(FundCompositionWindow):
    members: list[FundTopHolderMember]


class FinancialWindow(Body):
    source_code: StrictStr
    asset_type: Literal['fund', 'a-share']
    reported_timestamp_ms: StrictInt | None = Field(ge=0)
    reported_period: StrictStr | None
    requested_start: date | None
    requested_end: date | None
    reported_historical_revision_evidence: StrictBool | None
    coverage_basis: Literal['source_observation_window']
    comparable_units: None = None
    accounting_basis: None = None
    normalized_currency: None = None
    single_period_values: None = None
    as_filed_history: None = None
    complete_history: None = None


class FundFinancialIndicatorReport(CompositionMember):
    reported_asset_nav: ReportedSigned | None
    reported_average_nav_profit_margin: ReportedSigned | None
    reported_average_share_current_profit: ReportedSigned | None
    reported_current_income: ReportedSigned | None
    reported_current_profit: ReportedSigned | None
    reported_distribution_profit: ReportedSigned | None
    reported_distribution_share_profit: ReportedSigned | None
    reported_nav_rate: ReportedSigned | None
    reported_share_nav: ReportedSigned | None
    reported_sum_nav_rate: ReportedSigned | None
    reported_sum_share_nav: ReportedSigned | None
    reported_start_date: date | None
    reported_end_date: date | None
    reported_publish_date: date | None


class FundFinancialIndicatorWindow(FinancialWindow):
    asset_type: Literal['fund']
    reports: list[FundFinancialIndicatorReport]


class FundIncomeReport(CompositionMember):
    reported_bond_investment_income: ReportedSigned | None
    reported_custodian_fee: ReportedSigned | None
    reported_dividend_income: ReportedSigned | None
    reported_exchange_income: ReportedSigned | None
    reported_fair_value_income: ReportedSigned | None
    reported_fee: ReportedSigned | None
    reported_fund_investment_income: ReportedSigned | None
    reported_income: ReportedSigned | None
    reported_interest_income: ReportedSigned | None
    reported_investment_income: ReportedSigned | None
    reported_manager_reward: ReportedSigned | None
    reported_net_profit: ReportedSigned | None
    reported_other_income: ReportedSigned | None
    reported_stock_investment_income: ReportedSigned | None
    reported_tax_surcharge: ReportedSigned | None
    reported_total_fee: ReportedSigned | None
    reported_total_income: ReportedSigned | None
    reported_total_profit: ReportedSigned | None
    reported_transaction_cost: ReportedSigned | None
    reported_start_date: date | None
    reported_end_date: date | None
    reported_publish_date: date | None


class FundIncomeWindow(FinancialWindow):
    asset_type: Literal['fund']
    reports: list[FundIncomeReport]


class FundBalanceReport(CompositionMember):
    reported_bank_deposit: ReportedSigned | None
    reported_bond_investment: ReportedSigned | None
    reported_fund_investment: ReportedSigned | None
    reported_liability_and_owner_equity: ReportedSigned | None
    reported_other_assets: ReportedSigned | None
    reported_other_liability: ReportedSigned | None
    reported_owner_total_equity: ReportedSigned | None
    reported_stock_investment: ReportedSigned | None
    reported_total_assets: ReportedSigned | None
    reported_total_liability: ReportedSigned | None
    reported_transactional_financial_assets: ReportedSigned | None
    reported_undistributed_profit: ReportedSigned | None
    reported_start_date: date | None
    reported_end_date: date | None
    reported_publish_date: date | None


class FundBalanceWindow(FinancialWindow):
    asset_type: Literal['fund']
    reports: list[FundBalanceReport]


class StockIncomeReport(CompositionMember):
    reported_basic_eps: ReportedSigned | None
    reported_income_tax_expense: ReportedSigned | None
    reported_interest_expenses: ReportedSigned | None
    reported_manage_fee: ReportedSigned | None
    reported_net_profit: ReportedSigned | None
    reported_operating_costs: ReportedSigned | None
    reported_operating_expenses: ReportedSigned | None
    reported_operating_income: ReportedSigned | None
    reported_operating_profit: ReportedSigned | None
    reported_parent_holder_net_profit: ReportedSigned | None
    reported_profit_total: ReportedSigned | None
    reported_research_and_development_expenses: ReportedSigned | None
    reported_sales_fee: ReportedSigned | None
    reported_period_end: date | None
    reported_report_date: date | None
    reported_currency: StrictStr | None
    reported_fiscal_period: StrictStr | None
    reported_period: StrictStr | None
    reported_ticker: StrictStr | None
    reported_fiscal_year: StrictInt | None = Field(ge=0)


class StockIncomeWindow(FinancialWindow):
    asset_type: Literal['a-share']
    reports: list[StockIncomeReport]


class StockBalanceReport(CompositionMember):
    reported_accounts_receivable: ReportedSigned | None
    reported_assets_total: ReportedSigned | None
    reported_cash: ReportedSigned | None
    reported_holder_equity_total: ReportedSigned | None
    reported_non_current_nets_total: ReportedSigned | None
    reported_total_current_assets: ReportedSigned | None
    reported_total_debt: ReportedSigned | None
    reported_period_end: date | None
    reported_report_date: date | None
    reported_currency: StrictStr | None
    reported_fiscal_period: StrictStr | None
    reported_period: StrictStr | None
    reported_ticker: StrictStr | None
    reported_fiscal_year: StrictInt | None = Field(ge=0)


class StockBalanceWindow(FinancialWindow):
    asset_type: Literal['a-share']
    reports: list[StockBalanceReport]


class StockCashFlowReport(CompositionMember):
    reported_act_cash_flow_net: ReportedSigned | None
    reported_cash_equivalents_net_addition: ReportedSigned | None
    reported_financing_cash_flow_net: ReportedSigned | None
    reported_invest_cash_flow_net: ReportedSigned | None
    reported_pay_dividends_profits_interest_cash: ReportedSigned | None
    reported_pay_fixed_assets_etc_cash: ReportedSigned | None
    reported_period_end: date | None
    reported_report_date: date | None
    reported_currency: StrictStr | None
    reported_fiscal_period: StrictStr | None
    reported_period: StrictStr | None
    reported_ticker: StrictStr | None
    reported_fiscal_year: StrictInt | None = Field(ge=0)


class StockCashFlowWindow(FinancialWindow):
    asset_type: Literal['a-share']
    reports: list[StockCashFlowReport]


class StockIndicatorValue(Body):
    provider_indicator: StrictStr
    reported_value: ReportedSigned | None


class StockIndicatorAbility(Body):
    provider_ability: StrictStr
    indicators: list[StockIndicatorValue]


class StockIndicatorReport(Body):
    source_order: StrictInt = Field(ge=0)
    reported_period: StrictStr
    abilities: list[StockIndicatorAbility]


class StockIndicatorWindow(Body):
    source_code: StrictStr
    asset_type: Literal['a-share']
    reports: list[StockIndicatorReport]
    requested_start: date | None
    requested_end: date | None
    reported_historical_revision_evidence: StrictBool | None
    coverage_basis: Literal['source_observation_window']
    calculation_formula: None = None
    comparable_units: None = None
    resolved_indicator_taxonomy: None = None
    effective_period_boundaries: None = None
    as_filed_history: None = None


class PortfolioWindow(Body):
    source_code: StrictStr
    asset_type: Literal['fund']
    coverage_basis: Literal['source_observation_window']
    resolved_instrument_identities: None = None
    portfolio_complete: None = None
    transport_complete: None = None
    comparable_units: None = None
    normalized_weights: None = None
    as_filed_history: None = None


class CurrentPortfolioMember(CompositionMember):
    source_member_code: StrictStr | None
    reported_hold_ratio: ReportedSigned | None
    reported_period_increase_rate_pct: ReportedSigned | None
    reported_position_capital: ReportedSigned | None
    reported_position_count: ReportedSigned | None
    reported_security_market_value_rate_pct: ReportedSigned | None
    reported_start_date: date | None
    reported_end_date: date | None
    reported_modify_time: date | None
    reported_publish_date: date | None
    reported_asset_type: StrictStr | None
    reported_stock_name: StrictStr | None
    reported_ticker: StrictStr | None
    reported_investment_rank: StrictInt | None = Field(ge=0)


class HistoricalPortfolioMember(CompositionMember):
    source_member_code: StrictStr
    reported_hold_ratio: ReportedSigned | None
    reported_market_value: ReportedSigned | None
    reported_period_increase_pct: ReportedSigned | None
    reported_end_date: date | None
    reported_asset_type: StrictStr | None
    reported_name: StrictStr | None
    reported_report_type: StrictStr | None
    reported_ticker: StrictStr | None
    reported_rank: StrictInt | None = Field(ge=0)


class CurrentPortfolioWindow(PortfolioWindow):
    reported_total_bond_ratio_pct: ReportedSigned | None
    reported_total_fund_ratio_pct: ReportedSigned | None
    members: list[CurrentPortfolioMember]
    reported_concentration_ratio: ReportedSigned | None
    reported_stock_ratio_pct: ReportedSigned | None
    reported_total_stock_ratio_pct: ReportedSigned | None
    reported_main_industry: StrictStr | None
    reported_timestamp_ms: StrictInt | None = Field(ge=0)


class PortfolioPeriod(Body):
    report_key: StrictStr
    period_start: date
    period_end: date
    provider_report_type: Literal['quarter', 'annual', 'semiannual']
    reported_type_name: StrictStr | None


class HistoricalPortfolioReport(PortfolioPeriod):
    source_order: StrictInt = Field(ge=0)
    reported_timestamp_ms: StrictInt | None = Field(ge=0)
    members: list[HistoricalPortfolioMember]


class HistoricalPortfolioWindow(PortfolioWindow):
    reports: list[HistoricalPortfolioReport]
    report_directory: list[PortfolioPeriod]
    current_provider_directory: list[PortfolioPeriod] | None
    reported_historical_revision_evidence: StrictBool | None


class NarrativeWindow(Body):
    source_code: StrictStr
    coverage_basis: Literal['source_observation_window']
    complete_history: None = None
    verified_market_events: None = None


class FundNewsArticle(Body):
    provider_article_id: StrictStr
    reported_content_type: StrictStr | None
    reported_title: StrictStr | None
    reported_summary: StrictStr | None
    reported_source: StrictStr | None
    reported_url: StrictStr | None
    reported_image_url: StrictStr | None
    reported_author: StrictStr | None
    reported_publish_time: AwareDatetime
    reported_top: StrictBool | None
    reported_fields: list[StrictStr]


class FundNewsWindow(NarrativeWindow):
    articles: list[FundNewsArticle]
    requested_start: date
    requested_end: date
    reported_coverage: StrictStr | None
    reported_scope_truncated: StrictBool | None


class RankTrendPoint(Body):
    trading_date: date
    reported_ticker: StrictStr | None
    reported_rank: StrictInt | None = Field(ge=0)


class RankTrendWindow(NarrativeWindow):
    points: list[RankTrendPoint]
    requested_start: date
    requested_end: date
    selection_formula: None = None


class StockAnomalyNarrative(Body):
    source_order: StrictInt = Field(ge=0)
    reported_name: StrictStr | None
    reported_tag: StrictStr | None
    reported_keywords: list[StrictStr]
    reported_analysis: StrictStr | None


class StockAnomalyWindow(NarrativeWindow):
    narratives: list[StockAnomalyNarrative]


@dataclass(frozen=True)
class Schema:
    dataset: str
    name: str
    body: type[Body]
    identity_kind: str
    core_fields: tuple[str, ...]
    business_fields: tuple[str, ...] = ()
    limitations: tuple[str, ...] = (
        'source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified')
    date_field: str | None = None


SCHEMAS = {item.dataset: item for item in (
    *(
        Schema(dataset, label, SourceLocalTableFact, 'source_row',
               ('native_dataset', 'native_table', 'source_identity', 'source_row_hash',
                'reported_text', 'reported_numbers', 'reported_dates', 'reported_times',
                'reported_identifiers', 'reported_flags', 'reported_nested_hashes', 'nested_semantics'),
               limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                            'source_row_only', 'nested_json_hash_only', 'economic_identity_unverified',
                            'market_completeness_unverified'))
        for dataset, label in (
            ('operations.etf_mapping_audit', 'ETF代码映射审计观察'),
            ('operations.etf_daily_revision', 'ETF日线修订审计观察'),
            ('operations.corporate_action_source_fact', '公司行动来源事实观察'),
            ('operations.corporate_action_fact', '公司行动事实观察'),
            ('operations.corporate_action_coverage', '公司行动覆盖证据观察'),
            ('operations.trading_status_source_fact', '交易状态来源事实观察'),
            ('operations.trading_status_fact', '交易状态事实观察'),
            ('operations.trading_status_coverage', '交易状态覆盖证据观察'),
            ('operations.trading_status_revision', '交易状态修订审计观察'),
        )
    ),
    Schema('operations.empty_local_scope', '固定本地空范围结算', EmptyLocalScope, 'local_capture_scope',
           ('provider', 'native_dataset', 'scope_identity', 'capture_identity', 'fixed_rows', 'evidence_basis', 'semantics'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'fixed_local_capture_only', 'empty_capture_not_market_absence')),
    Schema('operations.import_progress', '本地批量导入进度观察', ImportProgress, 'import_channel',
           ('import_channel', 'source_scope', 'artifact', 'pending_requests', 'imported_subjects', 'total_subjects', 'semantics'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'import_progress_not_business_publication', 'artifact_bounds_not_market_completeness')),
    Schema('fund.news_window', '基金资讯完整观察', FundNewsWindow, 'asset:fund',
           ('source_code', 'articles', 'coverage_basis'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'source_reports_not_verified_market_events', 'source_window_only')),
    Schema('market.rank_trend_window', '个股排名走势观察', RankTrendWindow, 'asset:a-share',
           ('source_code', 'points', 'coverage_basis'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'source_reports_not_verified_market_events', 'source_window_only')),
    Schema('market.stock_anomaly_window', '个股异动叙述观察', StockAnomalyWindow, 'asset:a-share',
           ('source_code', 'narratives', 'coverage_basis'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'source_reports_not_verified_market_events', 'source_window_only')),

    Schema('fund.reported_holdings_window', '基金重仓持仓来源观察', CurrentPortfolioWindow, 'asset:fund',
           ('source_code', 'asset_type', 'members', 'coverage_basis'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'historical_member_identity_unresolved', 'portfolio_completeness_unverified')),
    Schema('fund.reported_stock_holdings_window', '基金股票持仓历史来源观察', HistoricalPortfolioWindow, 'asset:fund',
           ('source_code', 'asset_type', 'reports', 'coverage_basis'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'historical_member_identity_unresolved', 'portfolio_completeness_unverified')),
    Schema('fund.reported_bond_holdings_window', '基金债券持仓历史来源观察', HistoricalPortfolioWindow, 'asset:fund',
           ('source_code', 'asset_type', 'reports', 'coverage_basis'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'historical_member_identity_unresolved', 'portfolio_completeness_unverified')),

    Schema('market.financial_indicator_window', '股票财务指标完整观察', StockIndicatorWindow, 'asset:a-share',
           ('source_code', 'asset_type', 'reports', 'coverage_basis'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_indicator_formula_and_units_unverified', 'historical_vintages_unverified')),
    Schema('fund.financial_indicator_window', '基金财务指标完整观察', FundFinancialIndicatorWindow, 'asset:fund',
           ('source_code', 'asset_type', 'reports', 'coverage_basis'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_accounting_basis_unverified', 'historical_vintages_unverified')),
    Schema('fund.income_window', '基金利润表完整观察', FundIncomeWindow, 'asset:fund',
           ('source_code', 'asset_type', 'reports', 'coverage_basis'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_accounting_basis_unverified', 'historical_vintages_unverified')),
    Schema('fund.balance_window', '基金资产负债表完整观察', FundBalanceWindow, 'asset:fund',
           ('source_code', 'asset_type', 'reports', 'coverage_basis'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_accounting_basis_unverified', 'historical_vintages_unverified')),
    Schema('market.income_window', '股票利润表完整观察', StockIncomeWindow, 'asset:a-share',
           ('source_code', 'asset_type', 'reports', 'coverage_basis'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_accounting_basis_unverified', 'historical_vintages_unverified')),
    Schema('market.balance_window', '股票资产负债表完整观察', StockBalanceWindow, 'asset:a-share',
           ('source_code', 'asset_type', 'reports', 'coverage_basis'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_accounting_basis_unverified', 'historical_vintages_unverified')),
    Schema('market.cash_flow_window', '股票现金流量表完整观察', StockCashFlowWindow, 'asset:a-share',
           ('source_code', 'asset_type', 'reports', 'coverage_basis'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_accounting_basis_unverified', 'historical_vintages_unverified')),

    Schema('fund.allocation_window', '基金配置完整观察', FundAllocationWindow, 'asset:fund',
           ('source_code', 'asset_type', 'members', 'coverage_basis'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_units_unverified', 'source_occurrences_not_resolved')),
    Schema('fund.industry_window', '基金行业配置完整观察', FundIndustryWindow, 'asset:fund',
           ('source_code', 'asset_type', 'members', 'coverage_basis'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_units_unverified', 'source_occurrences_not_resolved')),
    Schema('fund.holder_composition_window', '基金持有人结构完整观察', FundHolderWindow, 'asset:fund',
           ('source_code', 'asset_type', 'members', 'coverage_basis'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_units_unverified', 'source_occurrences_not_resolved')),
    Schema('fund.top_holder_window', '基金主要持有人完整观察', FundTopHolderWindow, 'asset:fund',
           ('source_code', 'asset_type', 'members', 'coverage_basis'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_units_unverified', 'source_occurrences_not_resolved')),

    Schema('fund.dividend_window', '基金分红完整观察', FundDividendWindow, 'asset:fund',
           ('source_code', 'asset_type', 'events', 'coverage_basis'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'reported_events_not_execution', 'provider_units_and_tax_basis_unverified')),
    Schema('market.corporate_action_window', '股票除权除息完整观察', CorporateActionWindow, 'asset:a-share',
           ('source_code', 'asset_type', 'events', 'coverage_basis'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'reported_events_not_execution', 'source_occurrences_not_resolved')),
    Schema('fund.manager_performance_window', '基金经理业绩窗口', ManagerPerformanceWindow, 'manager_performance_window',
           ('manager_id','provider_period','points','coverage_basis'),limitations=('source_local_identity_only','observed_time_only',
           'historical_public_time_unverified','provider_period_and_formula_unverified','reported_series_not_proven_comparable')),
    Schema('fund.manager_style_snapshot', '基金经理风格观察', ManagerStyleSnapshot, 'fund_manager', ('manager_id','preferences'),
           limitations=('source_local_identity_only','observed_time_only','historical_public_time_unverified','provider_vector_labels_unverified','source_narrative_only')),
    Schema('fund.return_snapshot', '基金区间收益观察', ReturnSnapshot, 'asset:fund', ('source_code','asset_type','periods'),
           limitations=('source_local_identity_only','observed_time_only','historical_public_time_unverified','provider_period_and_formula_unverified','observed_scope_only')),
    Schema('fund.drawdown_snapshot', '基金最大回撤观察', DrawdownSnapshot, 'asset:fund', ('source_code','asset_type','periods'),
           limitations=('source_local_identity_only','observed_time_only','historical_public_time_unverified','provider_period_and_formula_unverified','observed_scope_only')),
    Schema('fund.performance_window', '基金历史指标窗口', PerformanceWindow, 'asset:fund', ('source_code','asset_type','points'),
           limitations=('source_local_identity_only','observed_time_only','historical_public_time_unverified','provider_period_and_formula_unverified','observed_scope_only')),
    Schema('market.dragon_tiger_snapshot', '龙虎榜完整观察', DragonTigerSnapshot, 'dragon_tiger_list',
           ('collection_key','trading_date','board_type','stocks','hot_money'),business_fields=('trading_date',),date_field='trading_date',
           limitations=('source_local_identity_only','observed_time_only','historical_public_time_unverified',
                        'provider_reported_semantics_only','source_occurrences_not_resolved','not_complete_market_universe')),
    Schema('market.auction_snapshot', '竞价终态观察', AuctionSnapshot, 'asset:a-share', ('source_code', 'asset_type', 'reported_phase', 'reported_status'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_reported_semantics_only', 'not_complete_market_universe')),
    Schema('market.auction_benchmark_snapshot', '竞价基准名单观察', AuctionBenchmarkSnapshot, 'auction_benchmark', ('collection_key', 'trading_date', 'scope_basis', 'members'),
           business_fields=('trading_date',), date_field='trading_date',
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_reported_semantics_only', 'not_complete_market_universe')),
    Schema('market.limit_up_snapshot', '涨停名单观察', LimitUpSnapshot, 'limit_up_list', ('collection_key', 'trading_date', 'scope_basis', 'members'),
           business_fields=('trading_date',), date_field='trading_date',
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_reported_semantics_only', 'not_complete_market_universe')),
    Schema('market.limit_down_snapshot', '跌停名单观察', LimitDownSnapshot, 'limit_down_list', ('collection_key', 'trading_date', 'scope_basis', 'members'),
           business_fields=('trading_date',), date_field='trading_date',
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_reported_semantics_only', 'not_complete_market_universe')),
    Schema('market.limit_break_snapshot', '炸板名单观察', LimitBreakSnapshot, 'limit_break_list', ('collection_key', 'trading_date', 'scope_basis', 'members'),
           business_fields=('trading_date',), date_field='trading_date',
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_reported_semantics_only', 'not_complete_market_universe')),
    Schema('market.anomaly_snapshot', '市场异动叙述观察', AnomalySnapshot, 'anomaly_list', ('collection_key', 'scope_basis', 'members'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_reported_semantics_only', 'not_complete_market_universe')),
    Schema('market.limit_ladder_window', '连板梯队观察窗口', LimitLadderWindow, 'limit_ladder', ('collection_key', 'coverage', 'days', 'declared_board_caps'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_reported_semantics_only', 'not_complete_market_universe')),
    Schema('market.stock_quote_snapshot', '股票行情快照观察', StockQuoteSnapshot, 'asset:a-share',
           ('source_code', 'asset_type', 'reported_value_status'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_timestamp_not_business_date', 'reported_values_only', 'units_and_formulas_unverified')),
    Schema('market.etf_quote_snapshot', 'ETF行情快照观察', EtfQuoteSnapshot, 'asset:fund-etf',
           ('source_code', 'asset_type', 'reported_value_status'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_timestamp_not_business_date', 'reported_values_only', 'units_and_formulas_unverified')),
    Schema('market.index_quote_snapshot', '指数行情快照观察', IndexQuoteSnapshot, 'asset:a-share-index',
           ('source_code', 'asset_type', 'reported_value_status'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_timestamp_not_business_date', 'reported_values_only', 'units_and_formulas_unverified')),
    Schema('market.stock_valuation_snapshot', '股票估值快照观察', StockValuationSnapshot, 'asset:a-share',
           ('source_code', 'asset_type', 'reported_value_status'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_timestamp_not_business_date', 'reported_values_only', 'units_and_formulas_unverified')),
    Schema('instrument.reference', '标的来源目录', InstrumentReference, 'asset', ('source_code', 'asset_type')),
    Schema('fund.company', '基金公司资料', FundCompany, 'fund_company', ('company_id',)),
    Schema('fund.manager', '基金经理资料', FundManager, 'fund_manager', ('manager_id',)),
    Schema('fund.profile', '基金基础资料', FundProfile, 'fund_share', ('source_code',)),
    Schema('market.popularity_snapshot', '热股排行集合观察', PopularitySnapshot, 'stock_rank_list',
           ('collection_key', 'period', 'scope_basis', 'members'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_returned_list_only', 'heat_method_unverified', 'not_complete_market_universe')),
    Schema('market.rising_popularity_snapshot', '飙升排行集合观察', PopularitySnapshot, 'stock_rank_list',
           ('collection_key', 'period', 'scope_basis', 'members'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_returned_list_only', 'heat_method_unverified', 'not_complete_market_universe')),
    Schema('market.popularity_history_snapshot', '历史热股排行集合观察', HistoricalPopularitySnapshot, 'stock_rank_list',
           ('collection_key', 'period', 'scope_basis', 'members', 'ranking_date'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_returned_list_only', 'heat_method_unverified', 'not_complete_market_universe'), date_field='ranking_date'),
    Schema('fund.quota_summary_snapshot', 'QDII分类额度汇总观察', QuotaSummarySnapshot, 'qdii_summary_category',
           ('collection_key', 'scope_basis', 'reported_groups'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'requested_category_only', 'quota_currency_units_and_formula_unverified',
                        'source_display_text_not_comparable_amounts', 'current_subscription_eligibility_unverified')),
    Schema('fund.quota_list_snapshot', 'QDII分类基金额度观察', QuotaListSnapshot, 'qdii_list_category',
           ('collection_key', 'scope_basis', 'reported_groups'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'requested_category_only', 'quota_currency_units_and_formula_unverified',
                        'source_display_text_not_comparable_amounts', 'current_subscription_eligibility_unverified')),
    Schema('market.stock_daily_window', '股票日线观察窗口', StockDailyWindow, 'asset:a-share',
           ('source_code', 'asset_type', 'reported_adjustment', 'coverage', 'sequence_start', 'sequence_end', 'points'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'observed_rows_not_full_requested_range', 'currency_volume_and_turnover_units_unverified',
                        'adjustment_anchor_and_formula_unverified', 'captured_numeric_precision_not_provider_precision')),
    Schema('market.etf_daily_window', 'ETF前复权日线观察窗口', EtfDailyWindow, 'asset:fund-etf',
           ('source_code', 'asset_type', 'reported_adjustment', 'coverage', 'sequence_start', 'sequence_end', 'points'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'observed_rows_not_full_requested_range', 'currency_volume_and_turnover_units_unverified',
                        'adjustment_anchor_and_formula_unverified', 'captured_numeric_precision_not_provider_precision')),
    Schema('market.index_daily_window', '指数日线观察窗口', IndexDailyWindow, 'asset:a-share-index',
           ('source_code', 'asset_type', 'reported_adjustment', 'coverage', 'sequence_start', 'sequence_end', 'points'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'observed_rows_not_full_requested_range', 'currency_volume_and_turnover_units_unverified',
                        'adjustment_anchor_and_formula_unverified', 'captured_numeric_precision_not_provider_precision')),
    Schema('fund.offering_snapshot', '基金募集集合观察', FundOfferingSnapshot, 'fund_offering_filter',
           ('collection_key', 'selection_basis', 'members'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_reported_subscription_windows_only', 'current_subscription_eligibility_unverified')),
    Schema('fund.nav_snapshot', '基金净值窗口观察', FundNavSnapshot, 'fund_share',
           ('source_code', 'coverage', 'value_basis', 'sequence_start', 'sequence_end', 'points'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'provider_reported_nav_categories_only', 'currency_and_share_basis_unverified',
                        'adjustment_formula_unverified', 'cumulative_nav_and_return_derivation_not_supported',
                        'rolling_window_not_complete_fund_history')),
    Schema('fund.manager_experience', '基金经理任职观察', ManagerExperience, 'fund_manager',
           ('manager_id', 'assignments'), limitations=('source_local_identity_only', 'observed_time_only',
           'historical_public_time_unverified', 'assignment_effective_dates_unverified',
           'performance_units_unverified')),
    Schema('index.category_snapshot', '指数分类目录快照', IndexCategorySnapshot, 'index_category',
           ('collection_key', 'collection_kind', 'membership_basis', 'members'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'membership_effective_dates_unverified', 'provider_reported_set_only')),
    Schema('index.constituent_snapshot', '指数成分集合快照', IndexConstituentSnapshot, 'asset:a-share-index',
           ('collection_key', 'collection_kind', 'membership_basis', 'members'),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'membership_effective_dates_unverified', 'provider_reported_set_only')),
    Schema('market.calendar', '交易日期', CalendarDay, 'calendar',
           ('exchange_scope', 'calendar_date', 'scope_basis'), ('calendar_date',), date_field='calendar_date'),
    Schema('market.adjustment_factor', '基金来源复权因子', AdjustmentFactor, 'asset:fund-etf',
           ('source_code', 'trade_date', 'factor', 'factor_basis'), ('trade_date',),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'adjustment_anchor_unverified', 'adjusted_price_derivation_not_supported'), date_field='trade_date'),
    Schema('market.fund_daily', '基金来源日线', FundDailyObservation, 'asset:fund-etf',
           ('source_code', 'trade_date', 'open', 'high', 'low', 'close', 'price_unit', 'price_basis'), ('trade_date',),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'share_quantity_conversion_not_supported', 'adjusted_price_derivation_not_supported'), date_field='trade_date'),
)}


# The historical ranking date was already stored and filterable in 1.0, but
# its contract omitted the business-time annotation because the day is part of
# the source-local subject key. A metadata-only minor preserves those keys.
DATE_METADATA_REVISIONS = {'market.popularity_history_snapshot': '1.1'}


def schema_for(dataset):
    try:
        return SCHEMAS[dataset]
    except KeyError:
        raise FoundationError('DOMAIN_NOT_IMPLEMENTED', '该领域的标准契约尚未实现，不能发布原始JSON冒充正式值。') from None


def validate_body(dataset, body):
    # Pydantic performs only date parsing here; all categorical and numerical
    # fields use strict scalar types. Canonical serialization happens after this
    # validation, so later reads do not need source decoding or unit inference.
    return schema_for(dataset).body.model_validate(body).model_dump(mode='json')
