"""Explicit milestone-two contracts and source-local validation.

Keep provider fields intact. Decimal JSON is serialized without converting to
binary floats; the read API declares decimal-as-string encoding to consumers.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
import json
import re
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

SHANGHAI = ZoneInfo("Asia/Shanghai")
FUND_TYPES = ("fund-etf", "fund-lof", "fund-reits", "fund-otc")
ASSET_TYPES = FUND_TYPES + ("a-share", "a-share-index")
CODE = re.compile(r"[A-Z0-9_-]{1,48}\.[A-Z]{2,8}\Z")


class CollectionError(ValueError):
    """Only locally authored explanations can enter persisted error messages."""


class CollectionParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_types: list[Literal["fund-etf", "fund-lof", "fund-reits", "fund-otc", "a-share", "a-share-index"]] = Field(default=list(ASSET_TYPES), min_length=1)
    subjects: list[str] | None = Field(default=None, min_length=1, max_length=10000)
    mode: Literal["incremental", "reconcile", "backfill"] = "incremental"
    batch_size: int = Field(default=100, ge=1, le=1000)
    refresh_today: bool = False
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def validate_scope(self):
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("开始日期不能晚于结束日期。")
        if self.end_date and self.end_date >= datetime.now(SHANGHAI).date():
            # Explicit date requests must be closed days. Daily jobs resolve
            # today's availability independently from this backfill control.
            raise ValueError("手动历史回补的结束日期须早于今天。")
        self.asset_types = sorted(set(self.asset_types))
        if self.subjects is not None:
            self.subjects = sorted(set(self.subjects))
            if any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", s) for s in self.subjects):
                raise ValueError("标的或关联对象 ID 格式不正确。")
        return self


@dataclass(frozen=True)
class Dataset:
    key: str
    name: str
    english_name: str
    interface: str
    assets: tuple[str, ...] = FUND_TYPES
    kind: str = "snapshot"
    frequency: str = "weekly"
    identity: str = "thscode"
    years: int = 0
    adjust: str | None = None
    allow_empty: bool = False
    variant: str = "default"


DATASETS = {d.key: d for d in (
    Dataset("tickers", "同花顺标的目录", "Tonghuashun ticker directory", "meta.tickers.list", ASSET_TYPES, "directory", "daily"),
    Dataset("fund_profile", "同花顺基金基础资料", "Tonghuashun fund profiles", "fund.profile.detail", kind="profile"),
    Dataset("fund_company", "同花顺基金公司资料", "Tonghuashun fund companies", "fund.companies.detail", kind="company", identity="company_id"),
    Dataset("fund_manager", "同花顺基金经理资料", "Tonghuashun fund managers", "fund.managers.detail", kind="manager", identity="manager_id"),
    Dataset("fund_manager_experience", "同花顺基金经理从业经历", "Tonghuashun manager experience", "fund.managers.experience", kind="manager", identity="manager_id"),
    Dataset("etf_daily", "同花顺ETF前复权日线", "Tonghuashun ETF forward-adjusted daily bars", "fund.market.historical", ("fund-etf",), "bars", "daily", years=5, adjust="forward"),
    Dataset("fund_nav", "同花顺基金净值", "Tonghuashun fund NAV", "fund.performance.nav", kind="nav", frequency="daily", years=5),
    Dataset("fund_dividends", "同花顺基金分红", "Tonghuashun fund dividends", "fund.corporate-actions.dividends", frequency="daily", allow_empty=True),
    Dataset("fund_holdings", "同花顺基金重仓持仓", "Tonghuashun fund holdings", "fund.portfolio.holdings", allow_empty=True),
    Dataset("fund_stock_history", "同花顺基金历史股票持仓", "Tonghuashun fund stock reports", "fund.portfolio.stock-history", kind="reports", allow_empty=True),
    Dataset("fund_bond_history", "同花顺基金历史债券持仓", "Tonghuashun fund bond reports", "fund.portfolio.bond-history", kind="reports", allow_empty=True),
    Dataset("fund_allocation", "同花顺基金资产配置", "Tonghuashun fund asset allocation", "fund.portfolio.asset-allocation"),
    Dataset("fund_industry", "同花顺基金行业配置", "Tonghuashun fund industry allocation", "fund.portfolio.industry-allocation"),
    Dataset("fund_holders", "同花顺基金持有人结构", "Tonghuashun fund holder structure", "fund.holders.detail"),
    Dataset("fund_top_holders", "同花顺基金前十大持有人", "Tonghuashun fund top holders", "fund.holders.top", allow_empty=True),
    Dataset("fund_financial_indicators", "同花顺基金财务指标", "Tonghuashun fund financial indicators", "fund.financials.indicators"),
    Dataset("fund_income", "同花顺基金利润表", "Tonghuashun fund income statements", "fund.financials.income-statements"),
    Dataset("fund_balance", "同花顺基金资产负债表", "Tonghuashun fund balance sheets", "fund.financials.balance-sheets"),
    Dataset("stock_daily", "同花顺A股未复权日线", "Tonghuashun A-share unadjusted daily bars", "a-share.prices.historical", ("a-share",), "bars", "daily", years=10, adjust="none"),
    Dataset("stock_actions", "同花顺A股除权除息事件", "Tonghuashun A-share corporate actions", "a-share.corporate-actions.adjustment-factors", ("a-share",), frequency="daily", allow_empty=True),
    Dataset("stock_income", "同花顺A股利润表", "Tonghuashun A-share income statements", "a-share.financials.income-statements", ("a-share",), "financials", "daily", years=10),
    Dataset("stock_balance", "同花顺A股资产负债表", "Tonghuashun A-share balance sheets", "a-share.financials.balance-sheets", ("a-share",), "financials", "daily", years=10),
    Dataset("stock_cash_flow", "同花顺A股现金流量表", "Tonghuashun A-share cash flow statements", "a-share.financials.cash-flow-statements", ("a-share",), "financials", "daily", years=10),
    Dataset("stock_indicators", "同花顺A股财务指标", "Tonghuashun A-share financial indicators", "a-share.financials.indicators", ("a-share",), "indicators", "daily", years=10),
    Dataset("stock_valuation", "同花顺A股估值快照", "Tonghuashun A-share valuations", "a-share.valuations.snapshot", ("a-share",), frequency="daily", identity="thscodes"),
    Dataset("calendar", "同花顺交易日历", "Tonghuashun trading calendar", "a-share.calendar.trading-days", (), "calendar", "daily"),
    Dataset("index_catalog", "同花顺指数分类目录", "Tonghuashun index categories", "a-share-index.catalog.ths-index-list", ("a-share-index",), "index_catalog", "daily", identity="tag"),
    Dataset("index_daily", "同花顺指数日线", "Tonghuashun index daily bars", "a-share-index.prices.historical", ("a-share-index",), "bars", "daily", years=10),
    Dataset("index_constituents", "同花顺指数成分快照", "Tonghuashun index constituent snapshots", "a-share-index.constituents.ths-stock-list", ("a-share-index",), frequency="daily"),
    Dataset("index_quote", "同花顺指数行情快照", "Tonghuashun index quote snapshots", "a-share-index.prices.snapshot", ("a-share-index",), frequency="daily", identity="thscodes"),
)}


def exact_json(value: Any) -> str:
    """Produce deterministic standard JSON without losing a decimal digit."""
    if isinstance(value, float):
        return exact_json(Decimal(str(value)))
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CollectionError("响应包含非有限数值。")
        # Keep a negative zero in the decimal JSON domain; a bare -0 would be
        # parsed as integer zero and change the content digest on reconstruction.
        return "-0.0" if str(value) == "-0" else str(value)
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + exact_json(v)
                              for k, v in sorted(value.items())) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(exact_json(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def content_hash(value: Any) -> str:
    return hashlib.sha256(exact_json(value).encode()).hexdigest()


def date_ms(value: date) -> int:
    return int(datetime.combine(value, time.min, SHANGHAI).timestamp() * 1000)


def provider_date(value: Any) -> date:
    if type(value) is not int:
        raise CollectionError("响应日期须为毫秒整数。")
    try:
        return datetime.fromtimestamp(value / 1000, SHANGHAI).date()
    except (OverflowError, OSError, ValueError):
        raise CollectionError("响应日期超出支持范围。") from None


def years_before(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def windows(start: date, end: date, years: int, *, overlap_days: int = 0):
    # Use a conservative day bound to satisfy both calendar-year and fixed-day
    # upstream validators. Adjacent windows cover every date exactly once.
    while start <= end:
        stop = min(end, start + timedelta(days=365 * years - 1))
        yield start, stop
        if stop == end:
            break
        start = stop + timedelta(days=1 - overlap_days)


def items(data: dict, *, allow_empty: bool = False) -> list[dict]:
    validate_source_dates(data)
    value = data.get("item")
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise CollectionError("同花顺响应缺少合法记录列表。")
    if not value and not allow_empty:
        raise CollectionError("同花顺返回空记录，尚不能确认数据已就绪。")
    return value


def validate_source_dates(value: Any):
    """Check documented millisecond fields without reinterpreting their role."""
    if isinstance(value, dict):
        for key, child in value.items():
            if (key.endswith("_ms") or key == "timestamp") and child is not None:
                provider_date(child)
            elif isinstance(child, (dict, list)):
                validate_source_dates(child)
    elif isinstance(value, list):
        for child in value:
            validate_source_dates(child)


def validate_ticker(row: dict, asset_type: str):
    code = row.get("thscode")
    if not isinstance(code, str) or not CODE.fullmatch(code):
        raise CollectionError("标的目录包含不完整或非法代码。")
    if row.get("asset_type") != asset_type:
        raise CollectionError("标的目录返回了请求范围以外的资产类型。")
    for key, maximum in (("name", 256), ("exchange", 16)):
        value = row.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > maximum):
            raise CollectionError("标的目录字段类型或长度不正确。")
    for key in ("list_date", "end_date", "last_trade_date", "last_delivery_date"):
        if row.get(key) is not None:
            try:
                date.fromisoformat(row[key])
            except (ValueError, TypeError):
                raise CollectionError("标的目录日期格式不正确。") from None


def validate_bars(rows: list[dict], start: date, end: date, date_field="date_ms"):
    seen = set()
    for row in rows:
        day = provider_date(row.get(date_field))
        if not start <= day <= end or day in seen:
            raise CollectionError("历史数据包含重复日期或范围外记录。")
        seen.add(day)
        if date_field == "nav_date":
            fields = ("unit_nav", "adj_nav")
        else:
            fields = ("open_price", "high_price", "low_price", "close_price", "volume", "turnover")
        for field in fields:
            value = row.get(field)
            if value is None and date_field == "nav_date":
                continue
            if isinstance(value, bool) or not isinstance(value, (int, Decimal)) or value < 0:
                raise CollectionError("行情或净值存在缺失、负值或非法数值。")
        if date_field == "nav_date":
            if all(row.get(f) is None for f in fields):
                raise CollectionError("净值记录未提供任何净值。")
        elif not (row["low_price"] <= min(row["open_price"], row["close_price"])
                  <= max(row["open_price"], row["close_price"]) <= row["high_price"]):
            raise CollectionError("行情最高价、最低价与开收盘价不一致。")
