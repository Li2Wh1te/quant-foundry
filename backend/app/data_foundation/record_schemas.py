"""Executable canonical schemas, not an unrestricted native-JSON publication.

Every admitted field has a type and an observation-level meaning. Unknown
quantitative units are represented by an unavailable optional field, never by
copying a number into a guessed monetary or percentage unit. New domains must
install an explicit schema/adapter before they can publish any canonical body.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator

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


class CalendarDay(Body):
    exchange_scope: StrictStr = Field(min_length=1, max_length=128)
    calendar_date: date
    is_open: StrictBool | None
    scope_basis: Literal['declared_exchange', 'provider_reported_dates']


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
    Schema('market.calendar', '交易日期', CalendarDay, 'calendar',
           ('exchange_scope', 'calendar_date', 'scope_basis'), ('calendar_date',), date_field='calendar_date'),
    Schema('market.adjustment_factor', '基金来源复权因子', AdjustmentFactor, 'asset:fund-etf',
           ('source_code', 'trade_date', 'factor', 'factor_basis'), ('trade_date',),
           limitations=('source_local_identity_only', 'observed_time_only', 'historical_public_time_unverified',
                        'adjustment_anchor_unverified', 'adjusted_price_derivation_not_supported'), date_field='trade_date'),
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
