"""Code-owned business maps compiled from declared registry/model paths.

No request selects SQL, source, representation or layout columns. These maps
preserve current supplier meaning; raw execution and adjusted-price claims
require independent evidence in their respective domain modules.
"""
from dataclasses import dataclass
from types import MappingProxyType

from app.data_store.adapters.contracts import digest
from app.data_store.adapters.registry import BY_ID, Entry
from .contracts import Units

ETF_CATALOG = 'etf-current'
ETF_DAILY = 'etf-daily-local'
ETF_FACTORS = 'etf-factors-local'
EXCHANGE_CALENDAR = 'exchange-calendar-local'


@dataclass(frozen=True)
class MarketBinding:
    key: str
    entry: Entry
    label: str
    fields: tuple[str, ...]
    subject_kind: str
    business_key: str
    price_basis: str | None = None
    units: tuple[str | None, str | None, str | None] = (None, None, None)
    variant: str = 'default'

    @property
    def representation(self) -> str:
        # Same explicit source namespace as LocalInput.representation_key.
        return self.entry.source + ':' + digest([self.entry.native, self.variant])[:24]

    @property
    def columns(self) -> dict[str, str]:
        model = self.entry.projection[-2]
        return {name: self.entry.layout.columns[model, name][0] for name in self.fields}

    @property
    def declared_units(self) -> Units:
        return Units(price=self.units[0], volume=self.units[1], amount=self.units[2])

    @property
    def contract_token(self) -> str:
        return digest([self.key, self.entry.spec.schema_id, self.entry.spec.rule,
                       self.representation, self.columns, self.subject_kind,
                       self.business_key, self.price_basis, self.units])

    def partition(self, subject: str, object_key: str) -> str:
        return self.entry.spec.partitioner((self.representation, subject, object_key, 'root'))


# E67 exposes only admitted formal fields. Fees/status/index/manager omitted
# by that formal model stay unknown; they are never fetched from etf_codes.
BINDINGS = MappingProxyType({
    ETF_CATALOG: MarketBinding(ETF_CATALOG, BY_ID['E67'], 'Tushare · 本地正式ETF资料',
        ('source_code', 'asset_type', 'name', 'exchange', 'currency', 'listing_date', 'end_date'),
        'asset:fund-etf', 'source_code'),
    EXCHANGE_CALENDAR: MarketBinding(EXCHANGE_CALENDAR, BY_ID['E68'], 'Tushare · 本地正式交易日历',
        ('exchange_scope', 'calendar_date', 'is_open', 'scope_basis'), 'exchange', 'calendar_date'),
    ETF_DAILY: MarketBinding(ETF_DAILY, BY_ID['E70'], 'Tushare · 本地正式日线（来源报告口径）',
        ('source_code', 'trade_date', 'open', 'high', 'low', 'close', 'price_unit', 'price_basis',
         'volume_lots', 'turnover_thousand_yuan'), 'asset:fund-etf', 'trade_date',
        price_basis='provider_reported', units=('元', '手', '千元')),
    ETF_FACTORS: MarketBinding(ETF_FACTORS, BY_ID['E69'], 'Tushare · 本地正式基金因子',
        ('source_code', 'trade_date', 'factor', 'factor_basis', 'anchor_date'), 'asset:fund-etf', 'trade_date'),
})

# Intentionally no E40 fallback and no E51 registration as raw. E70's existing
# provider_reported basis does not by itself prove an unadjusted price contract.
