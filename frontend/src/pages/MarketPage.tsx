import { CalendarDays, ChevronLeft, ChevronRight, RefreshCw, Search } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { marketClient, onMarketInvalidation } from "../features/market-data/data";
import { catalogSelection, createCatalogPager, ETF_CATALOG, MAX_CATALOG_PAGES } from "../features/market-data/catalog/paging";
import { useMarketResource } from "../features/market-data/catalog/useMarketResource";
import { useAuth } from "../auth/AuthContext";
import { ExchangeFilter } from "./market/ExchangeFilter";
import { MarketCalendar } from "./market/MarketCalendar";
import { MarketTable } from "./market/MarketTable";
import { exchangeName, LIST_STATES, MARKET_PATH, marketDate, marketFilters, marketQuery, syncPresentation, type MarketFilters } from "./market/marketPresentation";
import "./Market.css";

interface Position { key: string; top: number; left: number; mainTop: number; code: string }
const POSITION_KEY = "quant-foundry.market-position";
function readPosition(): Position | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(POSITION_KEY) || "null");
    return value && typeof value.key === "string" && typeof value.code === "string" && [value.top, value.left, value.mainTop].every((number: unknown) => typeof number === "number" && Number.isFinite(number) && number >= 0) ? value : null;
  } catch { return null; }
}

/** Independent current formal reads. Refresh and calendar actions only issue GET. */
export function MarketPage() {
  const { logout } = useAuth();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const serialized = params.toString();
  const filters = useMemo(() => marketFilters(new URLSearchParams(serialized)), [serialized]);
  const key = marketQuery(filters);
  const [query, setQuery] = useState(filters.keyword);
  const [revision, setRevision] = useState(0);
  const [calendar, setCalendar] = useState(params.get("calendar") === "open");
  const table = useRef<HTMLDivElement>(null);
  const root = useRef<HTMLElement>(null);
  const restore = useRef(readPosition());
  const restored = useRef(false);
  const previousKey = useRef(key);
  const pager = useMemo(() => createCatalogPager(), []);
  const onUnauthorized = useCallback(() => { logout(); navigate("/login", { replace: true }); }, [logout, navigate]);
  const pendingQuery = query.trim() !== filters.keyword.trim();
  const loadList = useCallback((signal: AbortSignal) => pager.load(filters, signal), [pager, key]);
  const loadOverview = useCallback((signal: AbortSignal) => marketClient.overview(ETF_CATALOG, { signal }), []);
  const list = useMarketResource(`${catalogSelection(filters)}:offset=${filters.offset}`, loadList, revision, onUnauthorized, !pendingQuery);
  const summary = useMarketResource(`overview:${ETF_CATALOG}`, loadOverview, revision, onUnauthorized);
  const result = list.data?.page ?? null;
  const overview = summary.data;
  const offset = list.data?.offset ?? filters.offset;
  const busy = pendingQuery || list.pending || (!result && !list.error);
  const sync = syncPresentation(overview);
  const returnTo = MARKET_PATH + (key ? `?${key}` : "");

  useEffect(() => onMarketInvalidation(() => pager.clear()), [pager]);
  useEffect(() => { setQuery(filters.keyword); }, [filters.keyword]);
  useEffect(() => { if (new URLSearchParams(serialized).get("calendar") === "open") setCalendar(true); }, [serialized]);
  useEffect(() => {
    if (!pendingQuery) return;
    pager.clear();
    const timer = window.setTimeout(() => setParams(marketQuery({ ...filters, keyword: query.trim(), offset: 0 }), { replace: true }), 250);
    return () => window.clearTimeout(timer);
  }, [pendingQuery, query, filters, setParams, pager]);
  useEffect(() => {
    // Replayed bookmark positions may be beyond a catalog that has shrunk.
    if (list.data && !list.stale && offset !== filters.offset) setParams(marketQuery({ ...filters, offset }), { replace: true });
  }, [list.data, list.stale, offset, filters, setParams]);
  useLayoutEffect(() => {
    if (!result || busy) return;
    const saved = restore.current;
    const main = root.current?.closest<HTMLElement>(".qfo-main");
    if (!restored.current && saved?.key === key) {
      table.current?.scrollTo(saved.left, saved.top); main?.scrollTo(0, saved.mainTop);
      const row = [...(table.current?.querySelectorAll<HTMLTableRowElement>("tr[data-code]") ?? [])].find(item => item.dataset.code === saved.code);
      row?.querySelector<HTMLAnchorElement>("a")?.focus({ preventScroll: true });
    } else if (previousKey.current !== key) table.current?.scrollTo(0, 0);
    restored.current = true; previousKey.current = key;
  }, [result, busy, key]);

  function change(patch: Partial<MarketFilters>) {
    setParams(marketQuery({ ...filters, keyword: query.trim(), offset: 0, ...patch }), { replace: true });
  }
  function remember(code: string) {
    if (!table.current) return;
    const value: Position = { key, top: table.current.scrollTop, left: table.current.scrollLeft, mainTop: root.current?.closest<HTMLElement>(".qfo-main")?.scrollTop ?? 0, code };
    // Store position only, never formal results, read tokens or cursors.
    try { sessionStorage.setItem(POSITION_KEY, JSON.stringify(value)); } catch { /* Optional position persistence. */ }
  }
  function refresh() { if (!list.pending && !pendingQuery) { pager.clear(); setRevision(value => value + 1); } }
  const shownPage = Math.floor(offset / filters.limit) + 1;
  const pageCount = result?.total == null ? "—" : Math.max(1, Math.ceil(result.total / filters.limit));
  const continuing = list.canContinue && !list.stale && !list.error;

  return <section ref={root} className="qfm-content" aria-labelledby="qfm-title">
    <header className="qfo-page-head qfm-page-head"><div><div className="qfo-eyebrow">ETF / CURRENT FORMAL DATA</div><h1 id="qfm-title">ETF 市场</h1><p className="qfo-page-desc">当前 ETF 目录的正式基础资料与交易日期事实。</p></div><div className="qfo-page-actions"><button className="qfo-secondary-btn" type="button" aria-haspopup="dialog" onClick={() => setCalendar(true)}><CalendarDays aria-hidden="true" />交易日历</button><button className="qfo-secondary-btn" type="button" disabled={list.pending || pendingQuery} aria-busy={list.pending} onClick={refresh}><RefreshCw className={list.pending ? "qfm-spin" : ""} aria-hidden="true" />重新读取</button></div></header>
    <section className="qfm-market-band" aria-label="ETF 目录概览" aria-busy={summary.pending}>
      <div className="qfm-metric"><div className="qfm-metric-label">交易所</div><div className="qfm-metric-value">{overview?.exchanges.map(exchangeName).join(" · ") || "—"}</div><div className="qfm-metric-sub">当前目录资料范围</div></div>
      <div className="qfm-metric"><div className="qfm-metric-label">上市 ETF</div><div className="qfm-metric-value">{overview?.listed_count?.toLocaleString("zh-CN") ?? "—"}</div><div className="qfm-metric-sub">正式资料未提供上市状态</div></div>
      <div className="qfm-metric"><div className="qfm-metric-label">上市日期范围</div><div className="qfm-metric-value qfm-mono">{overview?.first_list_date ? `${marketDate(overview.first_list_date)} → ${marketDate(overview.latest_list_date)}` : "—"}</div><div className="qfm-metric-sub">已知上市日期，不代表行情覆盖</div></div>
      <div className="qfm-metric"><div className="qfm-metric-label">{sync.label}</div><div className="qfm-metric-value qfm-mono">{sync.value}</div><div className="qfm-metric-sub">{summary.stale ? "旧资料 · 重新读取失败" : sync.note}</div></div>
    </section>
    {summary.error && <div className="qfm-error" role="alert">目录概览：{summary.error.message}{summary.stale && " 当前显示上次读取的旧概览。"}</div>}
    <section className="qfm-workspace" aria-labelledby="qfm-list-title"><header className="qfm-workspace-head"><h2 id="qfm-list-title">ETF 基础资料</h2><div className="qfm-workspace-meta">目录 ETF <strong>{overview?.total_instruments?.toLocaleString("zh-CN") ?? "—"}</strong> 只</div></header>
      {list.error && <div className="qfm-error" role="alert">{list.error.message}{filters.listStatus && " 当前目录缺少上市状态依据，请选择“全部”。"}{list.stale && " 当前显示同条件的旧资料，不能继续翻页。"}<button className="qfm-text-button" type="button" disabled={list.pending} onClick={refresh}>重新读取</button></div>}
      <div className="qfm-toolbar"><label className="qfm-search"><Search aria-hidden="true" /><input type="search" aria-label="搜索 ETF" placeholder="搜索基金代码或名称" maxLength={128} value={query} onChange={event => setQuery(event.target.value)} /></label><ExchangeFilter value={filters.exchange} onChange={exchange => change({ exchange })} allowAll /><div className="qfm-chips" role="group" aria-label="上市状态筛选">{LIST_STATES.map(([value, label]) => <button key={value} className="qfm-chip" type="button" aria-pressed={filters.listStatus === value} title={value ? "当前正式目录未提供此筛选依据" : undefined} onClick={() => change({ listStatus: value })}>{label}</button>)}</div><span className="qfm-toolbar-count" role="status">{busy ? "正在读取…" : result?.total != null ? `匹配 ${result.total.toLocaleString("zh-CN")} 只` : "尚未读取"}</span></div>
      <div className="qfm-source-note">当前目录未提供跟踪指数、上市状态、管理人和费率；缺失字段保持未知。</div>
      <div ref={table} className="qfm-table-wrap" tabIndex={0} role="region" aria-label="ETF 表格，可横向滚动"><MarketTable result={result} loading={busy} error={Boolean(list.error)} empty={overview?.total_instruments === 0} returnTo={returnTo} onOpen={remember} /></div>
      <footer className="qfm-pagination"><span>{result ? `显示 ${result.items.length ? offset + 1 : 0}–${offset + result.items.length} 只${result.total == null ? "，总数未核验" : `，共 ${result.total.toLocaleString("zh-CN")} 只`}` : "等待读取 ETF 资料"}</span><div className="qfm-pagination-controls"><label>每页 <select aria-label="每页条数" value={filters.limit} onChange={event => change({ limit: Number(event.target.value) })}>{[20, 50, 100].map(size => <option key={size} value={size}>{size} 只</option>)}</select></label><button className="qfo-icon-btn" type="button" aria-label="上一页" title="上一页" disabled={busy || !continuing || offset === 0} onClick={() => change({ offset: Math.max(0, offset - filters.limit) })}><ChevronLeft aria-hidden="true" /></button><span>{shownPage} / {pageCount}</span><button className="qfo-icon-btn" type="button" aria-label="下一页" title="下一页" disabled={busy || !continuing || !result?.next_cursor || shownPage >= MAX_CATALOG_PAGES} onClick={() => change({ offset: offset + filters.limit })}><ChevronRight aria-hidden="true" /></button></div></footer>
    </section>
    {shownPage >= MAX_CATALOG_PAGES && result?.next_cursor && <p className="qfm-source-note">当前分页会话已到容量上限，请缩小搜索或交易所范围。</p>}
    {result && <details className="qfm-read-details"><summary>正式资料读取说明{list.stale ? " · 旧资料" : ""}</summary><p>{result.meta.scope}。{result.meta.coverage.note}</p><p>来源：{result.meta.source_labels.join(" · ")}；业务截至日：{marketDate(result.meta.business_as_of)}。</p></details>}
    {calendar && <MarketCalendar onClose={() => { setCalendar(false); if (params.has("calendar")) setParams(key, { replace: true }); }} onUnauthorized={onUnauthorized} />}
  </section>;
}
