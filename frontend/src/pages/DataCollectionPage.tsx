import { CalendarDays, ChevronLeft, ChevronRight, Database, RefreshCw, Search } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import {
  DataCollectionApiError, EtfOverview, EtfPage, getEtfOverview,
  getTradingCalendarOverview, listEtfs, listTradingCalendarDays,
  TradingCalendarOverview, TradingCalendarPage
} from "../api/dataCollections";
import { useAuth } from "../auth/AuthContext";

const PAGE_SIZE = 50;

type CollectionPage = "trading-calendar" | "daily-quotes";
type OpenFilter = "" | "open" | "closed";

const EMPTY_PAGE: TradingCalendarPage = { items: [], total: 0, limit: PAGE_SIZE, offset: 0 };
const EMPTY_ETF_PAGE: EtfPage = { items: [], total: 0, limit: PAGE_SIZE, offset: 0 };

function formatDate(value: string | null): string {
  if (!value) return "—";
  const date = new Date(`${value}T00:00:00`);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit"
  }).format(date);
}

function formatTimestamp(value: string | null): string {
  if (!value) return "暂未采集";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false
  }).format(date);
}

function checkpointSummary(checkpoints: Record<string, string>): string {
  const values = Object.entries(checkpoints);
  if (values.length === 0) return "尚未建立检查点";
  return values.map(([exchange, date]) => `${exchange} 已推进至 ${date}`).join("；");
}

function DailyQuotesEmptyState() {
  return <section className="collection-page" aria-labelledby="collection-title">
    <div className="page-heading">
      <div><h2 id="collection-title">日线行情</h2><p>沪深 A 股日线行情采集数据</p></div>
    </div>
    <div className="collection-empty-state">
      <Database aria-hidden="true" />
      <h3>日线行情尚未接入</h3>
      <p>当前版本尚未配置日线行情的数据模型和采集任务，因此没有可展示的数据库数据。</p>
    </div>
  </section>;
}

function etfStatusLabel(value: string): string {
  return { L: "上市", D: "退市", P: "待上市" }[value] ?? value;
}

function etfExchangeLabel(value: string): string {
  return { SSE: "上交所", SZSE: "深交所", SH: "上交所", SZ: "深交所" }[value] ?? value;
}

function formatManagementFee(value: string | null): string {
  if (!value) return "—";
  const fee = Number(value);
  return Number.isFinite(fee) ? `${fee.toLocaleString("zh-CN", { maximumFractionDigits: 4 })}%` : value;
}

function etfRefreshSummary(overview: EtfOverview | null): string {
  if (!overview) return "正在加载 ETF 采集状态…";
  const refreshedAt = overview.refreshed_at ?? overview.last_updated_at;
  if (!refreshedAt) return "尚未完成 ETF 基础信息同步。";
  return `Tushare ETF 基础信息已于 ${formatTimestamp(refreshedAt)} 同步完成。`;
}

/**
 * Display persisted ETF reference data only after fetching it from the admin API.
 *
 * This page deliberately keeps source filtering server-side so that future data
 * providers cannot be mixed with Tushare records without an explicit UI choice.
 */
export function EtfBasicsPage() {
  const { logout } = useAuth();
  const navigate = useNavigate();
  const [overview, setOverview] = useState<EtfOverview | null>(null);
  const [result, setResult] = useState<EtfPage>(EMPTY_ETF_PAGE);
  const [keyword, setKeyword] = useState("");
  const [exchange, setExchange] = useState("");
  const [listStatus, setListStatus] = useState("");
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const hasLoadedRef = useRef(false);
  const latestRequestRef = useRef(0);

  const load = useCallback(async (background = false) => {
    const requestId = latestRequestRef.current + 1;
    latestRequestRef.current = requestId;
    const initialLoad = !hasLoadedRef.current;
    if (initialLoad && !background) setLoading(true); else setRefreshing(true);
    setError(null);
    try {
      const [nextOverview, nextResult] = await Promise.all([
        getEtfOverview(),
        listEtfs({
          keyword: keyword.trim() || undefined,
          exchange: exchange || undefined,
          listStatus: listStatus || undefined,
          limit: PAGE_SIZE,
          offset
        })
      ]);
      if (requestId !== latestRequestRef.current) return;
      setOverview(nextOverview);
      setResult(nextResult);
      hasLoadedRef.current = true;
    } catch (caught) {
      if (requestId !== latestRequestRef.current) return;
      if (caught instanceof DataCollectionApiError && caught.status === 401) {
        logout(); navigate("/login", { replace: true }); return;
      }
      setError(caught instanceof Error ? caught.message : "ETF 数据加载失败。");
    } finally {
      if (requestId === latestRequestRef.current) {
        setLoading(false); setRefreshing(false);
      }
    }
  }, [exchange, keyword, listStatus, logout, navigate, offset]);

  useEffect(() => { void load(); }, [load]);

  const currentPage = Math.floor(result.offset / PAGE_SIZE) + 1;
  const pageCount = Math.max(1, Math.ceil(result.total / PAGE_SIZE));
  const visibleStart = result.total === 0 ? 0 : result.offset + 1;
  const visibleEnd = Math.min(result.offset + result.items.length, result.total);
  const refreshTime = overview?.refreshed_at ?? overview?.last_updated_at ?? null;

  function resetFilters() {
    setKeyword(""); setExchange(""); setListStatus(""); setOffset(0);
  }

  return <section className="collection-page" aria-labelledby="collection-title">
    <div className="page-heading">
      <div><h2 id="collection-title">ETF 基础信息</h2><p>查看 ETF 标识、上市状态及跟踪指数等基础资料</p></div>
      <button className="toolbar-button" type="button" disabled={refreshing} onClick={() => void load(true)}>
        <RefreshCw className={refreshing ? "spin" : ""} aria-hidden="true" />刷新数据
      </button>
    </div>
    {error && <div className="page-error" role="alert">{error}</div>}
    <section className="collection-status" aria-labelledby="etf-status-title">
      <div className="collection-status__heading"><div><h3 id="etf-status-title">采集情况</h3><p>ETF 基础信息的数据库覆盖与最近同步状态</p></div><span>{formatTimestamp(refreshTime)} 更新</span></div>
      <div className="collection-status__stats">
        <div><span>上市日期范围</span><strong>{overview?.first_list_date ? `${formatDate(overview.first_list_date)} — ${formatDate(overview.latest_list_date)}` : "暂无数据"}</strong><small>仅统计已提供上市日期的 ETF</small></div>
        <div><span>数据库记录</span><strong>{(overview?.total_records ?? 0).toLocaleString("zh-CN")}</strong><small>共 {overview?.exchange_count ?? 0} 个交易所</small></div>
        <div><span>上市 ETF</span><strong>{(overview?.listed_count ?? 0).toLocaleString("zh-CN")}</strong><small>当前上市状态为上市</small></div>
      </div>
      <div className="collection-status__checkpoint"><CalendarDays aria-hidden="true" /><div><strong>最近同步</strong><span>{etfRefreshSummary(overview)}</span></div></div>
    </section>
    <section className="collection-table etf-table" aria-labelledby="etf-list-title">
      <div className="collection-table__heading"><div><h3 id="etf-list-title">ETF 列表</h3><span>筛选条件在服务端执行</span></div><strong aria-live="polite">{refreshing ? "正在更新…" : `${result.total.toLocaleString("zh-CN")} 条`}</strong></div>
      <div className="collection-filter collection-filter--etf" aria-label="ETF 筛选">
        <label>代码或名称<div className="etf-search-field"><Search aria-hidden="true" /><input type="search" value={keyword} placeholder="例如：510300 或 沪深300" onChange={(event) => { setKeyword(event.target.value); setOffset(0); }} /></div></label>
        <label>交易所<select value={exchange} onChange={(event) => { setExchange(event.target.value); setOffset(0); }}><option value="">全部交易所</option><option value="SSE">上交所</option><option value="SZSE">深交所</option></select></label>
        <label>上市状态<select value={listStatus} onChange={(event) => { setListStatus(event.target.value); setOffset(0); }}><option value="">全部状态</option><option value="L">上市</option><option value="D">退市</option><option value="P">待上市</option></select></label>
        <button type="button" onClick={resetFilters}>重置筛选</button>
      </div>
      <div className="collection-table__scroll"><table className="etf-data-table" aria-busy={loading || refreshing}><thead><tr><th>基金代码</th><th>名称</th><th>交易所</th><th>上市状态</th><th>上市日期</th><th>跟踪指数</th><th>管理人</th><th>管理费率</th><th>更新时间</th></tr></thead><tbody>{loading ? <tr><td colSpan={9} className="collection-table__empty">正在加载 ETF 数据…</td></tr> : result.items.length === 0 ? <tr><td colSpan={9} className="collection-table__empty">没有符合筛选条件的数据</td></tr> : result.items.map((etf) => <tr key={etf.ts_code}><td><Link className="etf-detail-link etf-code" to={`/admin/data/etf-basics/${encodeURIComponent(etf.ts_code)}`}>{etf.ts_code}</Link><small>{etf.etf_type ?? "—"}</small></td><td><Link className="etf-detail-link" to={`/admin/data/etf-basics/${encodeURIComponent(etf.ts_code)}`}>{etf.csname ?? etf.extname ?? "—"}</Link><small>{etf.cname ?? ""}</small></td><td>{etfExchangeLabel(etf.exchange)}</td><td><span className={`etf-status etf-status--${etf.list_status.toLowerCase()}`}>{etfStatusLabel(etf.list_status)}</span></td><td>{formatDate(etf.list_date)}</td><td><strong>{etf.index_name ?? "—"}</strong><small>{etf.index_code ?? ""}</small></td><td>{etf.mgr_name ?? "—"}</td><td>{formatManagementFee(etf.mgt_fee)}</td><td>{formatTimestamp(etf.updated_at)}</td></tr>)}</tbody></table></div>
      <div className="collection-table__pagination"><span>显示 {visibleStart}–{visibleEnd} 条，共 {result.total.toLocaleString("zh-CN")} 条</span><div><button type="button" aria-label="上一页" disabled={offset === 0} onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}><ChevronLeft aria-hidden="true" /></button><span>{currentPage} / {pageCount}</span><button type="button" aria-label="下一页" disabled={offset + PAGE_SIZE >= result.total} onClick={() => setOffset((value) => value + PAGE_SIZE)}><ChevronRight aria-hidden="true" /></button></div></div>
    </section>
  </section>;
}

export function DataCollectionPage({ page }: { page: CollectionPage }) {
  const { logout } = useAuth();
  const navigate = useNavigate();
  const [overview, setOverview] = useState<TradingCalendarOverview | null>(null);
  const [result, setResult] = useState<TradingCalendarPage>(EMPTY_PAGE);
  const [exchange, setExchange] = useState("");
  const [openFilter, setOpenFilter] = useState<OpenFilter>("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(page === "trading-calendar");
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (background = false) => {
    if (page !== "trading-calendar") return;
    if (background) setRefreshing(true); else setLoading(true);
    setError(null);
    try {
      const [nextOverview, nextResult] = await Promise.all([
        getTradingCalendarOverview(),
        listTradingCalendarDays({
          exchange: exchange || undefined,
          isOpen: openFilter === "" ? undefined : openFilter === "open",
          startDate: startDate || undefined,
          endDate: endDate || undefined,
          limit: PAGE_SIZE,
          offset
        })
      ]);
      setOverview(nextOverview);
      setResult(nextResult);
    } catch (caught) {
      if (caught instanceof DataCollectionApiError && caught.status === 401) {
        logout(); navigate("/login", { replace: true }); return;
      }
      setError(caught instanceof Error ? caught.message : "采集数据加载失败。");
    } finally {
      setLoading(false); setRefreshing(false);
    }
  }, [endDate, exchange, logout, navigate, offset, openFilter, page, startDate]);

  useEffect(() => { void load(); }, [load]);

  if (page === "daily-quotes") return <DailyQuotesEmptyState />;

  const currentPage = Math.floor(result.offset / PAGE_SIZE) + 1;
  const pageCount = Math.max(1, Math.ceil(result.total / PAGE_SIZE));
  const visibleStart = result.total === 0 ? 0 : result.offset + 1;
  const visibleEnd = Math.min(result.offset + result.items.length, result.total);

  function resetFilters() {
    setExchange(""); setOpenFilter(""); setStartDate(""); setEndDate(""); setOffset(0);
  }

  return <section className="collection-page" aria-labelledby="collection-title">
    <div className="page-heading">
      <div><h2 id="collection-title">交易日历</h2><p>查看采集覆盖情况及数据库中的全部交易日历记录</p></div>
      <button className="toolbar-button" type="button" disabled={refreshing} onClick={() => void load(true)}>
        <RefreshCw className={refreshing ? "spin" : ""} aria-hidden="true" />刷新数据
      </button>
    </div>
    {error && <div className="page-error" role="alert">{error}</div>}
    <section className="collection-status" aria-labelledby="collection-status-title">
      <div className="collection-status__heading"><div><h3 id="collection-status-title">采集情况</h3><p>交易日历采集任务已提交的数据覆盖与检查点状态</p></div><span>{formatTimestamp(overview?.last_updated_at ?? null)} 更新</span></div>
      <div className="collection-status__stats">
        <div><span>数据覆盖范围</span><strong>{overview?.start_date ? `${formatDate(overview.start_date)} — ${formatDate(overview.end_date)}` : "暂无数据"}</strong></div>
        <div><span>数据库记录</span><strong>{(overview?.total_records ?? 0).toLocaleString("zh-CN")}</strong><small>共 {overview?.exchange_count ?? 0} 个交易所</small></div>
        <div><span>交易日记录</span><strong>{(overview?.open_day_count ?? 0).toLocaleString("zh-CN")}</strong><small>按交易所分别统计</small></div>
      </div>
      <div className="collection-status__checkpoint"><CalendarDays aria-hidden="true" /><div><strong>检查点</strong><span>{overview ? checkpointSummary(overview.checkpoints) : "正在加载检查点…"}</span></div></div>
    </section>
    <section className="collection-table" aria-labelledby="calendar-data-title">
      <div className="collection-table__heading"><div><h3 id="calendar-data-title">交易日历数据</h3><span>筛选条件在服务端执行</span></div><strong>{result.total.toLocaleString("zh-CN")} 条</strong></div>
      <div className="collection-filter" aria-label="交易日历筛选">
        <label>交易所<select value={exchange} onChange={(event) => { setExchange(event.target.value); setOffset(0); }}><option value="">全部交易所</option><option value="SSE">上交所（SSE）</option><option value="SZSE">深交所（SZSE）</option></select></label>
        <label>交易状态<select value={openFilter} onChange={(event) => { setOpenFilter(event.target.value as OpenFilter); setOffset(0); }}><option value="">全部状态</option><option value="open">交易日</option><option value="closed">非交易日</option></select></label>
        <label>开始日期<input type="date" value={startDate} onChange={(event) => { setStartDate(event.target.value); setOffset(0); }} /></label>
        <label>结束日期<input type="date" value={endDate} onChange={(event) => { setEndDate(event.target.value); setOffset(0); }} /></label>
        <button type="button" onClick={resetFilters}>重置筛选</button>
      </div>
      <div className="collection-table__scroll"><table><thead><tr><th>日期</th><th>交易所</th><th>是否交易日</th><th>前一交易日</th><th>更新时间</th></tr></thead><tbody>{loading ? <tr><td colSpan={5} className="collection-table__empty">正在加载交易日历数据…</td></tr> : result.items.length === 0 ? <tr><td colSpan={5} className="collection-table__empty">没有符合筛选条件的数据</td></tr> : result.items.map((day) => <tr key={`${day.exchange}-${day.calendar_date}`}><td>{formatDate(day.calendar_date)}</td><td>{day.exchange}</td><td><span className={day.is_open ? "calendar-open" : "calendar-closed"}>{day.is_open ? "交易日" : "非交易日"}</span></td><td>{formatDate(day.previous_trading_date)}</td><td>{formatTimestamp(day.updated_at)}</td></tr>)}</tbody></table></div>
      <div className="collection-table__pagination"><span>显示 {visibleStart}–{visibleEnd} 条，共 {result.total.toLocaleString("zh-CN")} 条</span><div><button type="button" aria-label="上一页" disabled={offset === 0} onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}><ChevronLeft aria-hidden="true" /></button><span>{currentPage} / {pageCount}</span><button type="button" aria-label="下一页" disabled={offset + PAGE_SIZE >= result.total} onClick={() => setOffset((value) => value + PAGE_SIZE)}><ChevronRight aria-hidden="true" /></button></div></div>
    </section>
  </section>;
}
