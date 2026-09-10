import { CalendarDays, ChevronLeft, ChevronRight, RefreshCw, Search } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { DataCollectionApiError, getEtfOverview, listEtfs, type EtfOverview, type EtfPage } from "../api/dataCollections";
import { useAuth } from "../auth/AuthContext";
import { ExchangeFilter } from "./market/ExchangeFilter";
import { MarketCalendar } from "./market/MarketCalendar";
import { MarketTable } from "./market/MarketTable";
import { exchangeName, filterDescription, LIST_STATES, MARKET_PATH, marketDate, marketFilters, marketQuery, syncPresentation, type MarketFilters } from "./market/marketPresentation";
import "./Market.css";

interface Snapshot { overview: EtfOverview; result: EtfPage; filters: MarketFilters; key: string }
interface Position { key: string; top: number; left: number; mainTop: number; code: string }
const POSITION_KEY = "quant-foundry.market-position";

function readPosition(): Position | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(POSITION_KEY) || "null");
    return value && typeof value.key === "string" && typeof value.code === "string" && [value.top, value.left, value.mainTop].every((number: unknown) => typeof number === "number" && Number.isFinite(number) && number >= 0) ? value : null;
  } catch { return null; }
}

/** This page only queries collected facts. It never starts ingestion as a
 * side effect of refreshing, changing a filter, or opening the calendar. */
export function MarketPage() {
  const { logout } = useAuth();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const serialized = params.toString();
  const filters = useMemo(() => marketFilters(new URLSearchParams(serialized)), [serialized]);
  const key = marketQuery(filters);
  const [query, setQuery] = useState(filters.keyword);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [revision, setRevision] = useState(0);
  const [toast, setToast] = useState("");
  const [calendar, setCalendar] = useState(params.get("calendar") === "open");
  const table = useRef<HTMLDivElement>(null);
  const root = useRef<HTMLElement>(null);
  const restore = useRef(readPosition());
  const restored = useRef(false);
  const previousKey = useRef(key);
  const refreshRequested = useRef(false);
  const overviewCache = useRef<EtfOverview | null>(null);
  const onUnauthorized = useCallback(() => { logout(); navigate("/login", { replace: true }); }, [logout, navigate]);
  const pendingQuery = query.trim() !== filters.keyword.trim();
  const busy = loading || pendingQuery;
  const refreshing = loading && refreshRequested.current;
  // Pagination changes the visible slice, not the matching record count.
  // Keep that count while paging; a new search or filter must wait for its own total.
  const sameSelection = snapshot && filters.keyword.trim() === snapshot.filters.keyword.trim()
    && filters.exchange === snapshot.filters.exchange && filters.listStatus === snapshot.filters.listStatus;
  const countPending = busy && (pendingQuery || !sameSelection);
  const overview = snapshot?.overview ?? null;
  const result = snapshot?.result ?? null;
  const sync = syncPresentation(overview);
  const returnTo = MARKET_PATH + (key ? `?${key}` : "");

  useEffect(() => { setQuery(filters.keyword); }, [filters.keyword]);
  useEffect(() => { if (new URLSearchParams(serialized).get("calendar") === "open") setCalendar(true); }, [serialized]);
  useEffect(() => {
    if (!pendingQuery) return;
    // Cancel the old query before debouncing the next one, so an older response
    // cannot become a fresh-looking result while the user is still typing.
    const timer = window.setTimeout(() => {
      const next = marketQuery({ ...filters, keyword: query.trim(), offset: 0 });
      setParams(next, { replace: true });
    }, 250);
    return () => window.clearTimeout(timer);
  }, [pendingQuery, query, filters, setParams]);
  useEffect(() => {
    if (pendingQuery) return;
    const controller = new AbortController();
    const requestedFilters = marketFilters(new URLSearchParams(key));
    setLoading(true); setError(false); setToast("");
    // The market overview is independent of list filters and pagination.
    // Reload it only on entry or an explicit refresh, retaining an atomic snapshot.
    const overviewRequest = overviewCache.current && !refreshRequested.current
      ? Promise.resolve(overviewCache.current) : getEtfOverview(controller.signal);
    void Promise.all([overviewRequest, listEtfs(requestedFilters, controller.signal)])
      .then(([nextOverview, nextResult]) => {
        if (controller.signal.aborted) return;
        overviewCache.current = nextOverview;
        // A collection may shrink since the bookmarked page was visited.
        // Correct that position before presenting a misleading empty result.
        if (requestedFilters.offset > 0 && requestedFilters.offset >= nextResult.total) {
          setParams(marketQuery({ ...requestedFilters, offset: Math.max(0, Math.floor((nextResult.total - 1) / requestedFilters.limit)) * requestedFilters.limit }), { replace: true });
          return;
        }
        setSnapshot({ overview: nextOverview, result: nextResult, filters: requestedFilters, key });
        if (refreshRequested.current) { setToast("数据已刷新"); refreshRequested.current = false; }
      }).catch(caught => {
        if (controller.signal.aborted) return;
        if (caught instanceof DataCollectionApiError && caught.status === 401) onUnauthorized();
        else { setError(true); refreshRequested.current = false; }
      }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [key, revision, pendingQuery, setParams, onUnauthorized]);
  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(""), 2200);
    return () => window.clearTimeout(timer);
  }, [toast]);

  useLayoutEffect(() => {
    if (!snapshot || snapshot.key !== key) return;
    const saved = restore.current;
    const main = root.current?.closest<HTMLElement>(".qfo-main");
    if (!restored.current && saved?.key === key) {
      table.current?.scrollTo(saved.left, saved.top); main?.scrollTo(0, saved.mainTop);
      const row = [...(table.current?.querySelectorAll<HTMLTableRowElement>("tr[data-code]") ?? [])].find(item => item.dataset.code === saved.code);
      row?.querySelector<HTMLAnchorElement>("a")?.focus({ preventScroll: true });
    } else if (previousKey.current !== key) table.current?.scrollTo(0, 0);
    restored.current = true; previousKey.current = key;
  }, [snapshot, key]);

  function change(patch: Partial<MarketFilters>) {
    refreshRequested.current = false;
    setParams(marketQuery({ ...filters, keyword: query.trim(), offset: 0, ...patch }), { replace: true });
  }
  function remember(code: string) {
    if (!table.current) return;
    const value: Position = { key: snapshot?.key ?? key, top: table.current.scrollTop, left: table.current.scrollLeft, mainTop: root.current?.closest<HTMLElement>(".qfo-main")?.scrollTop ?? 0, code };
    try { sessionStorage.setItem(POSITION_KEY, JSON.stringify(value)); } catch { /* URL filters still work when optional position storage is unavailable. */ }
  }
  function refresh() { if (!busy) { refreshRequested.current = true; setRevision(value => value + 1); } }
  const shownPage = result ? Math.floor(result.offset / result.limit) + 1 : 1;
  const pageCount = result ? Math.max(1, Math.ceil(result.total / result.limit)) : 1;
  const loadedReturnTo = MARKET_PATH + (snapshot?.key ? `?${snapshot.key}` : "");
  // aria-disabled announces pending actions without dimming controls on every
  // page change. The handlers block duplicate mouse and keyboard activation.

  return <section ref={root} className="qfm-content" aria-labelledby="qfm-title">
    <header className="qfo-page-head qfm-page-head"><div><div className="qfo-eyebrow">CHINA A-SHARE MARKET</div><h1 id="qfm-title">A 股市场</h1><p className="qfo-page-desc">查看 A 股市场基础资料与 ETF 数据。</p></div><div className="qfo-page-actions"><button className="qfo-secondary-btn" type="button" aria-haspopup="dialog" onClick={() => setCalendar(true)}><CalendarDays aria-hidden="true" />交易日历</button><button className="qfo-secondary-btn" type="button" disabled={(!snapshot && busy) || refreshing} aria-disabled={busy} aria-busy={refreshing} onClick={refresh}><RefreshCw className={refreshing ? "qfm-spin" : ""} aria-hidden="true" />刷新数据</button></div></header>
    <section className="qfm-market-band" aria-label="市场数据概览">
      <div className="qfm-metric"><div className="qfm-metric-label">交易所</div><div className="qfm-metric-value">{overview ? overview.exchanges.map(exchangeName).join(" · ") || "—" : "—"}</div><div className="qfm-metric-sub">{overview ? overview.exchanges.join(" / ") || "尚无交易所记录" : "等待加载"}</div></div>
      <div className="qfm-metric"><div className="qfm-metric-label">上市 ETF</div><div className="qfm-metric-value">{overview?.listed_count.toLocaleString("zh-CN") ?? "—"}</div><div className="qfm-metric-sub">当前上市状态</div></div>
      <div className="qfm-metric"><div className="qfm-metric-label">ETF 数据覆盖</div><div className="qfm-metric-value qfm-mono">{overview?.first_list_date ? `${marketDate(overview.first_list_date)} → ${marketDate(overview.latest_list_date)}` : "—"}</div><div className="qfm-metric-sub">上市日期范围</div></div>
      <div className="qfm-metric"><div className="qfm-metric-label">{sync.label}</div><div className="qfm-metric-value qfm-mono">{sync.value}</div><div className="qfm-metric-sub">{sync.note}</div></div>
    </section>
    <section className="qfm-workspace" aria-labelledby="qfm-list-title"><header className="qfm-workspace-head"><h2 id="qfm-list-title">ETF 基础资料</h2><div className="qfm-workspace-meta">ETF 记录 <strong>{overview?.total_records.toLocaleString("zh-CN") ?? "—"}</strong> 条</div></header>
      {error && <div className="qfm-error" role="alert">{snapshot ? "刷新失败，当前保留上次成功加载的数据。" : "ETF 数据加载失败，请检查连接后重试。"}{snapshot && snapshot.key !== key && <span>上次筛选：{filterDescription(snapshot.filters)}。</span>}<button className="qfm-text-button" type="button" disabled={busy} onClick={refresh}>重新加载</button></div>}
      <div className="qfm-toolbar"><label className="qfm-search"><Search aria-hidden="true" /><input type="search" aria-label="搜索 ETF" placeholder="搜索基金代码、名称或跟踪指数" maxLength={200} value={query} onChange={event => setQuery(event.target.value)} /></label><ExchangeFilter value={filters.exchange} onChange={exchange => change({ exchange })} allowAll /><div className="qfm-chips" role="group" aria-label="上市状态筛选">{LIST_STATES.map(([value, label]) => <button key={value} className="qfm-chip" type="button" aria-pressed={filters.listStatus === value} onClick={() => change({ listStatus: value })}>{label}</button>)}</div><span className="qfm-toolbar-count" role="status">{countPending ? "正在加载…" : result ? `显示 ${result.total.toLocaleString("zh-CN")} 条` : "尚未加载"}</span></div>
      <div ref={table} className="qfm-table-wrap" tabIndex={0} role="region" aria-label="ETF 表格，可横向滚动"><MarketTable result={result} loading={busy} error={error} empty={overview?.total_records === 0} returnTo={snapshot ? loadedReturnTo : returnTo} onOpen={remember} /></div>
      <footer className="qfm-pagination"><span>{result ? `显示 ${result.total ? result.offset + 1 : 0}–${Math.min(result.offset + result.items.length, result.total)} 条，共 ${result.total.toLocaleString("zh-CN")} 条` : "等待加载 ETF 数据"}</span><div className="qfm-pagination-controls"><label>每页 <select aria-label="每页条数" value={filters.limit} onChange={event => change({ limit: Number(event.target.value) })}>{[20, 50, 100].map(size => <option key={size} value={size}>{size} 条</option>)}</select></label><button className="qfo-icon-btn" type="button" aria-label="上一页" title="上一页" disabled={error || !result || result.offset === 0} aria-disabled={busy || error || !result || result.offset === 0} onClick={() => { if (!busy) change({ offset: Math.max(0, filters.offset - filters.limit) }); }}><ChevronLeft aria-hidden="true" /></button><span>{shownPage} / {pageCount}</span><button className="qfo-icon-btn" type="button" aria-label="下一页" title="下一页" disabled={error || !result || result.offset + result.limit >= result.total} aria-disabled={busy || error || !result || result.offset + result.limit >= result.total} onClick={() => { if (!busy) change({ offset: filters.offset + filters.limit }); }}><ChevronRight aria-hidden="true" /></button></div></footer>
    </section>
    {toast && <div className="qfo-toast qfo-show qfm-toast" role="status">{toast}</div>}
    {calendar && <MarketCalendar onClose={() => { setCalendar(false); if (params.has("calendar")) setParams(key, { replace: true }); }} onUnauthorized={onUnauthorized} />}
  </section>;
}
