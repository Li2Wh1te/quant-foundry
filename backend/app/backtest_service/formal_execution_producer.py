"""Read narrowly typed daily execution facts from the existing versioned store.

The producer does not collect, infer or write facts. Its package is separate
from the old ETF rule package: old rule fields, source observations, account
metadata and caller acceptance flags cannot supply these inputs. The original
Rust composer remains responsible for execution/rate validation.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from collections.abc import Mapping
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import exists, or_, select
from sqlalchemy.orm import aliased

from app.instruments.domain import VersionedReference
from app.instruments.rules.contracts import canonical_payload
from .serialization import encoded
from .session_fact_pages import ExecutionSessionInput

PACKAGE = VersionedReference(key='s3_daily_execution_inputs', version=1)
PRODUCER_BRIDGES = frozenset({'daily_execution_inputs', 'daily_market_inputs'})
Label = Annotated[str, Field(min_length=1, max_length=256, pattern=r'\S')]
Evidence = Annotated[str, Field(min_length=1, max_length=1024, pattern=r'\S')]
DecimalText = Annotated[str, Field(pattern=r'^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$', max_length=128)]


class FactBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Listing(FactBody):
    first_trading_date: date
    last_trading_date: date | None
    verified_through: date
    kind: Literal['ipo', 'relisting', 'other']
    session_ordinal: Annotated[int, Field(gt=0, le=2**32-1)]


class Limited(FactBody):
    kind: Literal['limited']
    rate: DecimalText
    lower: DecimalText
    upper: DecimalText


class NoLimit(FactBody):
    kind: Literal['no_limit']
    reason: Literal['ipo', 'relisting', 'delisting_first_session', 'exchange_notice']
    session_ordinal: Annotated[int, Field(gt=0, le=2**32-1)]
    basis: Evidence


class TradingDay(FactBody):
    listing: Listing
    status: Literal['trading', 'halted']
    risk: Literal['normal', 'risk_warning', 'delisting']
    price_limit: Annotated[Limited | NoLimit, Field(discriminator='kind')]
    fund_price_band: Literal['ten_percent', 'twenty_percent']
    underlying_same_session: bool | None
    delisting_session_ordinal: Annotated[int, Field(gt=0, le=2**32-1)] | None
    submission_reference: DecimalText | None
    evidence: Evidence


class Permissions(FactBody):
    buy: bool
    sell: bool
    product_buy: bool
    risk_disclosure: bool | None
    delisting_buy: bool | None
    risk_bought_or_open: DecimalText | None


class Account(FactBody):
    account_id: UUID
    account_version: Annotated[int, Field(gt=0)]
    permissions: Permissions
    investor: Literal['resident_individual', 'resident_enterprise']
    sale_proceeds: Literal['immediate', 'next_session']
    sale_proceeds_basis: Evidence
    disposal_settlement_date: date | None
    holding_bands: Annotated[dict[date, Literal['up_to_one_month', 'over_month_up_to_year', 'over_one_year']], Field(max_length=10000)]
    evidence: Evidence


class Market(FactBody):
    entry_id: Literal['E70']
    representation: Annotated[str, Field(pattern=r'^tushare:[0-9a-f]{24}$')]
    source_subject: Label
    price_currency: Literal['CNY']
    price_basis: Literal['raw']
    quantity_unit: Literal['lots']
    shares_per_lot: DecimalText
    completed_bar_visibility: Literal['at_verified_session_close']
    evidence: Evidence


class DailyExecutionFact(FactBody):
    date: date
    product: Literal['equity_etf', 'bond_etf', 'money_etf', 'gold_etf',
                     'commodity_etf', 'cross_border_etf']
    day: TradingDay
    account: Account
    market: Market


class InputFactFault(ValueError):
    def __init__(self, code, *, fields=()):
        super().__init__(code)
        self.code = code
        self.details = dict(package=f'{PACKAGE.key}@{PACKAGE.version}', fields=list(fields))


REQUIRED_FIELDS = ('date', 'product', 'day.listing', 'day.status', 'day.risk',
    'day.price_limit', 'day.fund_price_band', 'day.underlying_same_session',
    'day.delisting_session_ordinal', 'day.submission_reference', 'day.evidence',
    'account.account_id', 'account.account_version', 'account.permissions',
    'account.investor', 'account.sale_proceeds', 'account.sale_proceeds_basis',
    'account.disposal_settlement_date', 'account.holding_bands', 'account.evidence',
    'market.entry_id', 'market.representation', 'market.source_subject',
    'market.price_currency', 'market.price_basis', 'market.quantity_unit',
    'market.shares_per_lot', 'market.completed_bar_visibility', 'market.evidence')


def captured_commission_schedule(schedule):
    """Adapt the existing account HTTP map[str,str] without changing its API.

    Only the saved explicit commission agreement is decoded. Other metadata
    cannot supply execution permissions, source semantics or inferred fees.
    The original commission validator still validates all resulting values.
    """
    if not isinstance(schedule, Mapping):
        return schedule
    metadata = schedule.get('metadata', {})
    if not isinstance(metadata, Mapping):
        return schedule
    agreement = metadata.get('s3_commission')
    if not isinstance(agreement, str):
        return schedule
    if len(agreement.encode()) > 4096:
        raise ValueError('saved_commission_agreement_too_large')
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('saved_commission_agreement_duplicate_field')
            result[key] = value
        return result
    try:
        decoded = json.loads(agreement, object_pairs_hook=unique_pairs)
    except ValueError:
        raise ValueError('saved_commission_agreement_invalid_json') from None
    if type(decoded) is not dict or set(decoded) - {'included_components', 'settlement_scale', 'rounding'}:
        raise ValueError('saved_commission_agreement_fields_invalid')
    return dict(schedule, metadata=dict(metadata, s3_commission=decoded))


def daily_rows(session, instrument_ids, day, cutoff):
    """Fold visible versions before applying validity, including removals."""
    from app.instruments.rule_facts_models import InstrumentRuleFactRecord as Row
    newer = aliased(Row)
    latest = ~exists(select(newer.id).where(newer.fact_key == Row.fact_key,
        newer.fact_version > Row.fact_version, newer.known_at <= cutoff, newer.observed_at <= cutoff))
    return tuple(session.scalars(select(Row).where(Row.instrument_id.in_(instrument_ids),
        Row.rule_package_key == PACKAGE.key, Row.rule_package_version == PACKAGE.version,
        Row.known_at <= cutoff, Row.observed_at <= cutoff, latest,
        Row.valid_from <= day, or_(Row.valid_to.is_(None), Row.valid_to > day))))


@dataclass(frozen=True)
class MarketInput:
    security: str
    instrument_id: UUID
    market: Market


def produce(rows, *, work, symbol, day, identity, calendar, cutoff):
    """Return original DTO fields only from one verified, account-bound fact."""
    from app.instruments.rule_facts_repository import _to_domain
    from app.instruments.rules.contracts import FactQualityStatus
    if not rows:
        raise InputFactFault('daily_execution_fact_missing', fields=REQUIRED_FIELDS)
    snapshot = work.specification.get('account_snapshot', {})
    selected = []
    for row in rows:
        candidate = _to_domain(row)  # includes content-hash and immutable-version validation
        if (candidate.quality_status != FactQualityStatus.COMPLETE or candidate.fixture_only
                or not candidate.source_revision or candidate.known_at > cutoff
                or candidate.observed_at > cutoff):
            raise InputFactFault('daily_execution_provenance_invalid')
        try:
            value = DailyExecutionFact.model_validate_json(encoded(canonical_payload(candidate.fields)))
        except ValidationError as error:
            fields = tuple('.'.join(map(str, item['loc'])) for item in error.errors())[:32]
            raise InputFactFault('daily_execution_fields_invalid', fields=fields) from None
        if (value.date != day or candidate.valid_from != day
                or candidate.valid_to != day + timedelta(days=1)):
            raise InputFactFault('daily_execution_date_or_validity_invalid', fields=('date', 'valid_from', 'valid_to'))
        if str(value.account.account_id) == snapshot.get('id') and value.account.account_version == snapshot.get('version'):
            selected.append(value)
    if len(selected) != 1:
        raise InputFactFault('daily_execution_account_missing_or_ambiguous', fields=('account.account_id', 'account.account_version'))
    value = selected[0]
    if (identity.asset_class != 'etf' or identity.currency != 'CNY'
            or identity.exchange not in ('SSE', 'SZSE') or value.market.source_subject != symbol):
        raise InputFactFault('daily_execution_identity_conflict')
    listing = value.day.listing
    if (listing.first_trading_date > day or listing.verified_through < day
            or listing.last_trading_date is not None and listing.last_trading_date < day):
        raise InputFactFault('daily_execution_listing_coverage_missing', fields=('day.listing',))
    from decimal import Decimal
    conversion = Decimal(value.market.shares_per_lot)
    if (conversion <= 0 or len(conversion.as_tuple().digits) > 38
            or not -38 <= conversion.as_tuple().exponent <= 0):
        raise InputFactFault('daily_execution_share_conversion_invalid', fields=('market.shares_per_lot',))
    trade_day = value.day.model_dump(mode='json', exclude={'evidence', 'submission_reference'})
    trade_day.update(security=symbol, date=day.isoformat(),
        session=calendar['fact'].calendar_id + ':' + day.isoformat())
    account = value.account.model_dump(mode='json', exclude={'account_id', 'account_version', 'evidence'})
    payload = dict(instrument=dict(security=symbol,
        exchange={'SSE': 'shanghai', 'SZSE': 'shenzhen'}[identity.exchange], product=value.product),
        day=trade_day, submission_reference=value.day.submission_reference, **account)
    return ExecutionSessionInput(identity.instrument_id, payload), MarketInput(symbol, identity.instrument_id, value.market)


class MarketAdmission:
    """Retain only one small source/identity mapping per authorized security."""
    def __init__(self, symbols):
        self.symbols, self.identities, self.contract, self.fault = tuple(symbols), {}, None, None

    def add(self, rows):
        if self.fault is not None:
            return
        for item in rows:
            market = item.market
            contract = (market.entry_id, market.representation, market.shares_per_lot)
            identity = (market.source_subject, str(item.instrument_id))
            if (self.contract is not None and self.contract != contract
                    or item.security in self.identities and self.identities[item.security] != identity):
                self.fault = InputFactFault('daily_market_contract_or_identity_changed')
                return
            self.contract = contract
            self.identities[item.security] = identity
        if len(encoded(self.identities)) > 4 * 1024**2:
            from .runtime_contracts import RuntimeFault
            raise RuntimeFault('RESOURCE_LIMIT', '默认行情身份映射超过缓存字节预算')

    def binding(self, calendar):
        if self.fault is not None:
            raise self.fault
        if self.contract is None or set(self.identities) != set(self.symbols):
            return None
        from .formal_runtime import daily_node_binding, REQUIRED_EXECUTION_FACTS
        entry, representation, conversion = self.contract
        return daily_node_binding(entry, representation=representation,
            identities=self.identities, calendar=calendar['calendar'], shares_per_quantity=conversion,
            accepted_facts=REQUIRED_EXECUTION_FACTS,
            evidence=dict(package=f'{PACKAGE.key}@{PACKAGE.version}', kind='validated_daily_execution_facts'))
