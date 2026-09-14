"""Reviewed REST endpoint inventory from Fuyao documentation (2026-09-14).

This inventory is not the scheduler registry and does not claim account access,
verified payload contracts, local datasets, or backtest compatibility. There
are 91 individually documented GET endpoints plus 3 API-key dump URL endpoints.
Planned stock basics, historical index membership/weights and reverse index
membership have no public route and are deliberately excluded. AI-client-only
"coming soon" notices do not disable their documented REST endpoints.
"""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Interface:
    key: str
    name: str
    english_name: str
    path: str
    documentation_url: str
    group: str
    method: str = "GET"
    verification_status: str = "not_verified"
    ingestion_status: str = "not_implemented"


# Paths are an explicit allowlist, not user-supplied URLs. Read-only discovery
# never invokes these endpoints or treats documentation as a permission probe.
_ROWS = (
    ('/api/a-share/valuations/snapshot', 'A股估值快照', 'valuations'),
    ('/api/a-share/financials/indicators', '财务指标数据', 'financial-indicators'),
    ('/api/a-share/prices/snapshot', '行情快照', 'prices'),
    ('/api/a-share/prices/historical', '历史 K 线', 'prices'),
    ('/api/meta/tickers/search', '标的检索', 'ticker-search'),
    ('/api/a-share/corporate-actions/adjustment-factors', '除复权', 'corporate-actions'),
    ('/api/meta/tickers/list', '标的列表获取', 'ticker-list'),
    ('/api/a-share/financials/income-statements', '利润表', 'financials'),
    ('/api/a-share/financials/balance-sheets', '资产负债表', 'financials'),
    ('/api/a-share/financials/cash-flow-statements', '现金流量表', 'financials'),
    ('/api/a-share/calendar/trading-days', '交易日历', 'calendar'),
    ('/api/a-share-index/catalog/ths-index-list', '同花顺指数列表', 'a-share-index'),
    ('/api/a-share-index/constituents/ths-stock-list', '同花顺指数成分股', 'a-share-index'),
    ('/api/a-share-index/prices/snapshot', '指数行情快照', 'a-share-index'),
    ('/api/a-share-index/prices/historical', '指数历史 K 线', 'a-share-index'),
    ('/api/a-share/special-data/anomaly-analysis-list', '个股异动原因列表', 'anomaly-analysis'),
    ('/api/a-share/special-data/anomaly-analysis-stock', '按股票查询个股异动原因', 'anomaly-analysis'),
    ('/api/a-share/auction/snapshot', 'A股集合竞价快照', 'auction'),
    ('/api/a-share/auction/short-term-benchmark', '短线风向标竞价基准', 'auction'),
    ('/api/a-share/capital-flow/snapshot', '资金流向实时快照', 'capital-flow'),
    ('/api/a-share/capital-flow/historical', '资金流向历史', 'capital-flow'),
    ('/api/a-share/special-data/dragon-tiger-list', '龙虎榜数据', 'dragon-tiger-data'),
    ('/api/fund/backtest/result', '基金在线回测', 'fund-backtest'),
    ('/api/fund/backtest/indicators', '基金回测可用指标', 'fund-backtest'),
    ('/api/fund/companies/detail', '基金公司详情', 'fund-company'),
    ('/api/fund/corporate-actions/dividends', '基金分红记录', 'fund-corporate-actions'),
    ('/api/fund/diagnostics/detail', '基金诊断详情', 'fund-diagnostics'),
    ('/api/fund/financials/indicators', '基金财务指标', 'fund-financials'),
    ('/api/fund/financials/income-statements', '基金利润表', 'fund-financials'),
    ('/api/fund/financials/balance-sheets', '基金资产负债表', 'fund-financials'),
    ('/api/fund/holders/detail', '基金持有人结构', 'fund-holders'),
    ('/api/fund/holders/top', '基金前十大持有人', 'fund-holders'),
    ('/api/fund/portfolio/holdings', '基金重仓持仓', 'fund-holdings'),
    ('/api/fund/indicators/line', '基金画线指标', 'fund-indicators'),
    ('/api/fund/indicators/table', '基金表格指标', 'fund-indicators'),
    ('/api/fund/managers/investment-style', '投资风格', 'fund-managers'),
    ('/api/fund/managers/performance', '基金经理业绩', 'fund-managers'),
    ('/api/fund/managers/experience', '从业经历', 'fund-managers'),
    ('/api/fund/managers/detail', '基金经理详情', 'fund-managers'),
    ('/api/fund/market/snapshot', '场内基金行情快照', 'fund-market'),
    ('/api/fund/market/historical', '场内基金历史日线行情', 'fund-market'),
    ('/api/fund/news/article-list', '基金资讯列表', 'fund-news'),
    ('/api/fund/offerings/list', '基金募集列表', 'fund-offerings'),
    ('/api/fund/performance/nav', '基金净值', 'fund-performance'),
    ('/api/fund/performance/returns', '基金区间收益', 'fund-performance'),
    ('/api/fund/performance/indicators-historical', '基金历史业绩指标', 'fund-performance'),
    ('/api/fund/performance/drawdowns', '基金最大回撤', 'fund-performance'),
    ('/api/fund/portfolio/stock-history', '基金历史股票持仓', 'fund-portfolio'),
    ('/api/fund/portfolio/bond-history', '基金历史债券持仓', 'fund-portfolio'),
    ('/api/fund/portfolio/stock-report-dates', '基金股票持仓报告日期', 'fund-portfolio'),
    ('/api/fund/portfolio/bond-report-dates', '基金债券持仓报告日期', 'fund-portfolio'),
    ('/api/fund/portfolio/asset-allocation', '基金资产配置', 'fund-portfolio'),
    ('/api/fund/portfolio/industry-allocation', '基金行业配置', 'fund-portfolio'),
    ('/api/fund/profile/detail', '基金基本资料', 'fund-profile'),
    ('/api/fund/quota/summary', 'QDII额度汇总', 'fund-quota'),
    ('/api/fund/quota/list', 'QDII额度列表', 'fund-quota'),
    ('/api/futures/basis/main-continuous-latest', '期货主连最新基差', 'futures-basis'),
    ('/api/futures/basis/historical', '期货历史基差', 'futures-basis'),
    ('/api/futures/calendar/trading-schedule', '期货交易日日程', 'futures-calendar'),
    ('/api/futures/variety-plates/list', '期货品种板块', 'futures-contracts-extended'),
    ('/api/futures/contracts/main-continuous-list', '期货主连资料', 'futures-contracts-extended'),
    ('/api/futures/contracts/main-list', '期货主力合约', 'futures-contracts-extended'),
    ('/api/futures/contracts/secondary-main-list', '期货次主力合约', 'futures-contracts-extended'),
    ('/api/futures/contracts/commodity-index-list', '期货商品指数资料', 'futures-contracts-extended'),
    ('/api/futures/fundamentals/indicators-historical', '期货F10历史指标', 'futures-fundamentals'),
    ('/api/futures/positions/variety-daily', '期货品种日持仓', 'futures-positions'),
    ('/api/futures/positions/company-variety-daily', '公司品种日持仓', 'futures-positions'),
    ('/api/futures/positions/contract-daily', '期货合约日持仓', 'futures-positions'),
    ('/api/futures/positions/contract-historical', '期货合约历史持仓', 'futures-positions'),
    ('/api/futures/positions/company-list', '期货公司列表', 'futures-positions'),
    ('/api/futures/prices/intraday', '期货当日分时', 'futures-prices'),
    ('/api/futures/prices/daily', '期货日K', 'futures-prices'),
    ('/api/futures/varieties/list', '期货品种资料', 'futures-reference'),
    ('/api/futures/contracts/detail', '期货合约详情', 'futures-reference'),
    ('/api/futures/calendar/session-timeline', '期货会话时间轴', 'futures-session-timeline'),
    ('/api/futures/warehouse-receipts/historical', '期货历史仓单', 'futures-warehouse-receipts'),
    ('/api/a-share/high-frequency/historical', '高频历史', 'high-frequency'),
    ('/api/a-share/high-frequency/intraday', '单日高频分时', 'high-frequency'),
    ('/api/a-share/special-data/skyrocket-list', '飙升榜', 'hot-list-data'),
    ('/api/a-share/special-data/hot-stock-list', 'A股热股榜单', 'hot-list-data'),
    ('/api/a-share/special-data/hot-stock-list-history', '历史热股排行', 'hot-list-data'),
    ('/api/a-share/special-data/hot-stock-rank-trend', '个股排名走势', 'hot-list-data'),
    ('/api/a-share/special-data/limit-up-pool', '涨停股票池', 'limit-up-data'),
    ('/api/a-share/special-data/limit-down-pool', '跌停股票池', 'limit-up-data'),
    ('/api/a-share/special-data/limit-break-pool', '炸板股票池', 'limit-up-data'),
    ('/api/a-share/special-data/limit-up-ladder', '连板天梯', 'limit-up-data'),
    ('/api/options/prices/intraday', '期权当日分时', 'options-prices'),
    ('/api/options/prices/daily', '期权日K', 'options-prices'),
    ('/api/options/varieties/list', '期权品种资料', 'options-reference'),
    ('/api/options/contracts/detail', '期权合约详情', 'options-reference'),
    ('/api/options/calendar/session-timeline', '期权会话时间轴', 'options-session-timeline'),
    ('/api/dump/market-dumps/daily-k/download-url', 'A股十年日线导出', 'market-dumps'),
    ('/api/dump/market-dumps/daily-k-10d/download-url', 'A股近期日线导出', 'market-dumps'),
    ('/api/dump/market-dumps/adjustment-factors/download-url', 'A股复权事件导出', 'market-dumps'),
)

INTERFACES = {}
for path, name, document in _ROWS:
    parts = path.removeprefix("/api/").split("/")
    key = ".".join(parts)
    INTERFACES[key] = Interface(key, name,
        " ".join(parts).replace("-", " ").title(), path,
        f"https://fuyao.aicubes.cn/docs/api-reference/{document}/", parts[0])


def list_interfaces() -> list[dict]:
    """Return independent public metadata objects; never return credentials."""
    return [asdict(item) for item in INTERFACES.values()]
