"""Executable canonical schemas, not an unrestricted native-JSON publication.

Every admitted field has a type and an observation-level meaning. Unknown
quantitative units are represented by an unavailable optional field, never by
copying a number into a guessed monetary or percentage unit. New domains must
install an explicit schema/adapter before they can publish any canonical body.
"""
from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

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


SCHEMAS = {item.dataset: item for item in (
    Schema('instrument.reference', '标的来源目录', InstrumentReference, 'asset', ('source_code', 'asset_type')),
    Schema('fund.company', '基金公司资料', FundCompany, 'fund_company', ('company_id',)),
    Schema('fund.manager', '基金经理资料', FundManager, 'fund_manager', ('manager_id',)),
    Schema('fund.profile', '基金基础资料', FundProfile, 'fund_share', ('source_code',)),
    Schema('market.calendar', '交易日期', CalendarDay, 'calendar',
           ('exchange_scope', 'calendar_date', 'scope_basis'), ('calendar_date',)),
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
