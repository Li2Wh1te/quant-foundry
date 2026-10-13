"""Code-owned current-display policy, separate from source observations.

Production E69 is deliberately not licensed here. The sole enabled cumulative
policy is explicit test-server configuration AND a distinct formal variant.
Nothing in an HTTP request supplies a policy, source, representation or anchor.
"""
from dataclasses import dataclass
from datetime import date

from app.data_store.schema import fingerprint
from .bindings import ETF_DAILY, TEST_ETF_DAILY, TEST_ETF_FACTORS
from .contracts import Anchor
from .errors import MarketError

RAW_POLICY = 'tushare-fund-daily-unadjusted@1'
RAW_NOTE = '本地正式 fund_daily 成交价，不施加复权；当前修订数据，不恢复历史当时所知。'
TEST_CONTRACT = 'cumulative-exact-date-fixture@1'
TEST_NOTE = '明示人工隔离契约：P×F/F(anchor)，仅精确同日因子有效，不前填；当前修订数据。不是E69生产公式依据，不恢复历史当时所知。'


@dataclass(frozen=True)
class PricePolicy:
    series_key: str
    raw_policy: str
    raw_note: str
    factor_key: str | None = None
    adjustment_policy: str | None = None
    adjustment_note: str | None = None
    post_date: date | None = None
    post_factor: str | None = None

    def token_for(self, adjustment):
        fields = {'series_key': self.series_key, 'raw_policy': self.raw_policy, 'raw_note': self.raw_note}
        if adjustment != 'none':
            fields.update(factor_key=self.factor_key, adjustment_policy=self.adjustment_policy,
                          adjustment_note=self.adjustment_note)
        if adjustment == 'post':
            fields.update(post_date=self.post_date, post_factor=self.post_factor)
        return fingerprint(fields)

    def post_anchor(self, as_of):
        if self.post_date is None or self.post_factor is None or self.post_date > as_of:
            raise MarketError('FACTOR_ANCHOR_UNVERIFIED')
        return Anchor(date=self.post_date, factor=self.post_factor)


def price_policy(settings, series_key):
    if series_key == ETF_DAILY:
        return PricePolicy(ETF_DAILY, RAW_POLICY, RAW_NOTE)
    if (series_key == TEST_ETF_DAILY and settings.environment == 'test'
            and settings.market_test_adjustment_contract == TEST_CONTRACT):
        return PricePolicy(TEST_ETF_DAILY, 'isolated-fixture-unadjusted@1', TEST_NOTE,
            TEST_ETF_FACTORS, 'isolated-cumulative-exact-date@1', TEST_NOTE,
            settings.market_test_post_anchor_date, settings.market_test_post_anchor_factor)
    raise MarketError('CAPABILITY_UNAVAILABLE')
