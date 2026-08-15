import { CalendarDays, ChevronLeft, ChevronRight, Database, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import {
  DataCollectionApiError, getTradingCalendarOverview, listTradingCalendarDays,
  TradingCalendarOverview, TradingCalendarPage
} from "../api/dataCollections";
import { useAuth } from "../auth/AuthContext";

const PAGE_SIZE = 50;

type CollectionPage = "trading-calendar" | "daily-quotes";
type OpenFilter = "" | "open" | "closed";

const EMPTY_PAGE: TradingCalendarPage = { items: [], total: 0, limit: PAGE_SIZE, offset: 0 };

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
