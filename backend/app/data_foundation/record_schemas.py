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
