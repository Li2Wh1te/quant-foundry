import { ChevronLeft, Expand, RefreshCw } from "lucide-react";
import { CandlestickSeries, ColorType, CrosshairMode, createChart, HistogramSeries, LineSeries, Time } from "lightweight-charts";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

import {
  DataCollectionApiError,
  EtfAdjustmentFactor,
  EtfCode,
  EtfDailyBar,
  getEtf,
  listEtfAdjustmentFactors,
  listEtfDailyBars
} from "../api/dataCollections";
import { useAuth } from "../auth/AuthContext";

type DetailTab = "basic" | "daily" | "factors";
type ChartPeriod = "day" | "week" | "month" | "year";
type AdjustmentMode = "raw" | "forward" | "backward";
const MOVING_AVERAGE_LENGTHS = [5, 10, 20] as const;
type MovingAverageLength = (typeof MOVING_AVERAGE_LENGTHS)[number];

interface NumericBar {
  tradeDate: string;
  open: number;
  high: number;
  low: number;
  close: number;
  vol: number;
  amount: number;
}

const TABS: { key: DetailTab; label: string }[] = [
  { key: "basic", label: "基础信息" },
  { key: "daily", label: "日线 K 线" },
  { key: "factors", label: "复权因子" }
];

const PERIOD_LABELS: Record<ChartPeriod, string> = {
  day: "日",
  week: "周",
  month: "月",
  year: "年"
};

const PERIOD_KLINE_LABELS: Record<ChartPeriod, string> = {
  day: "日 K 线",
  week: "周 K 线",
  month: "月 K 线",
  year: "年 K 线"
};

function formatDate(value: string | null): string {
  if (!value) return "—";
  const date = new Date(`${value}T00:00:00`);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit"
  }).format(date);
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false
  }).format(date);
}

function exchangeLabel(value: string): string {
  return { SSE: "上交所", SZSE: "深交所", SH: "上交所", SZ: "深交所" }[value] ?? value;
}

function statusLabel(value: string): string {
  return { L: "上市", D: "退市", P: "待上市" }[value] ?? value;
}

function decimal(value: string): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function numberText(value: string, fractionDigits = 4): string {
  const parsed = decimal(value);
  return parsed === null ? value : parsed.toLocaleString("zh-CN", { maximumFractionDigits: fractionDigits });
}

function dateKey(value: string, period: ChartPeriod): string {
  if (period === "day") return value;
  if (period === "month") return value.slice(0, 7);
  if (period === "year") return value.slice(0, 4);
  const date = new Date(`${value}T00:00:00Z`);
  const offset = (date.getUTCDay() + 6) % 7;
  date.setUTCDate(date.getUTCDate() - offset);
  return date.toISOString().slice(0, 10);
}

function toNumericBars(bars: EtfDailyBar): NumericBar | null {
  const values = [bars.open, bars.high, bars.low, bars.close, bars.vol, bars.amount].map(decimal);
  if (values.some((value) => value === null)) return null;
  const [open, high, low, close, vol, amount] = values as number[];
  return { tradeDate: bars.trade_date, open, high, low, close, vol, amount };
}

function adjustedBars(
  sourceBars: EtfDailyBar[],
  factors: EtfAdjustmentFactor[],
  adjustment: AdjustmentMode
): NumericBar[] | null {
  const rawBars = sourceBars.map(toNumericBars).filter((bar): bar is NumericBar => bar !== null);
  if (adjustment === "raw") return rawBars;

  // Tushare factors are normalized against the selected range endpoint so that
  // forward-adjusted prices end at the raw latest price and backward-adjusted
  // prices start at the raw earliest price.
  const factorByDate = new Map(factors.map((factor) => [factor.trade_date, decimal(factor.adj_factor)]));
  const matched = rawBars.map((bar) => ({ bar, factor: factorByDate.get(bar.tradeDate) }));
  if (matched.some(({ factor }) => factor === null || factor === undefined)) return null;

  const anchor = adjustment === "forward" ? matched.at(-1)?.factor : matched[0]?.factor;
  if (!anchor || anchor <= 0) return null;
  return matched.map(({ bar, factor }) => {
    const multiplier = factor! / anchor;
    return {
      ...bar,
      open: bar.open * multiplier,
      high: bar.high * multiplier,
      low: bar.low * multiplier,
      close: bar.close * multiplier
    };
  });
}

function aggregateBars(bars: NumericBar[], period: ChartPeriod): NumericBar[] {
  // Aggregation happens after adjustment.  This preserves the selected price
  // basis while applying the conventional OHLC and volume roll-up rules.
  const buckets = new Map<string, NumericBar[]>();
  for (const bar of bars) {
    const key = dateKey(bar.tradeDate, period);
    buckets.set(key, [...(buckets.get(key) ?? []), bar]);
  }
  return [...buckets.values()].map((bucket) => ({
    tradeDate: bucket.at(-1)!.tradeDate,
    open: bucket[0].open,
    high: Math.max(...bucket.map((bar) => bar.high)),
    low: Math.min(...bucket.map((bar) => bar.low)),
    close: bucket.at(-1)!.close,
    vol: bucket.reduce((sum, bar) => sum + bar.vol, 0),
    amount: bucket.reduce((sum, bar) => sum + bar.amount, 0)
  }));
}

function withinDateRange(value: string, startDate: string, endDate: string): boolean {
  return (!startDate || value >= startDate) && (!endDate || value <= endDate);
}

function movingAverage(bars: NumericBar[], length: number): (number | null)[] {
  return bars.map((_, index) => {
    if (index + 1 < length) return null;
    const closes = bars.slice(index + 1 - length, index + 1).map((bar) => bar.close);
    return closes.reduce((sum, value) => sum + value, 0) / length;
  });
}

function InteractiveKLineChart({
  bars,
  historyBars,
  visibleCount,
  visibleAverages
}: {
  bars: NumericBar[];
  historyBars: NumericBar[];
  visibleCount: number | null;
  visibleAverages: Record<MovingAverageLength, boolean>;
}) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const chart = createChart(container, {
      width: Math.max(container.clientWidth, 1),
      height: Math.max(container.clientHeight, 1),
      layout: { background: { type: ColorType.Solid, color: "#1d2025" }, textColor: "#aab4c4" },
      grid: { vertLines: { color: "#2b3037" }, horzLines: { color: "#343940" } },
      crosshair: { mode: CrosshairMode.MagnetOHLC },
      rightPriceScale: { borderColor: "#343940" },
      timeScale: {
        borderColor: "#343940",
        timeVisible: true,
        secondsVisible: false,
        fixLeftEdge: true,
        fixRightEdge: true
      }
    });
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: "#f35b69", downColor: "#45cc91", borderUpColor: "#f35b69", borderDownColor: "#45cc91", wickUpColor: "#f35b69", wickDownColor: "#45cc91"
    });
    candles.setData(bars.map((bar) => ({ time: bar.tradeDate as Time, open: bar.open, high: bar.high, low: bar.low, close: bar.close })));
    const volume = chart.addSeries(HistogramSeries, { priceFormat: { type: "volume" } }, 1);
    volume.setData(bars.map((bar) => ({ time: bar.tradeDate as Time, value: bar.vol, color: bar.close >= bar.open ? "rgba(243, 91, 105, 0.72)" : "rgba(69, 204, 145, 0.72)" })));
    chart.panes()[1].setHeight(92);

    // Moving averages are calculated from the complete loaded history, then
    // projected onto the current viewport so date filtering never restarts MA.
    const historyMa5 = movingAverage(historyBars, 5);
    const historyMa10 = movingAverage(historyBars, 10);
    const historyMa20 = movingAverage(historyBars, 20);
    const historyAverages = new Map(historyBars.map((bar, index) => [bar.tradeDate, {
      ma5: historyMa5[index], ma10: historyMa10[index], ma20: historyMa20[index]
    }]));
    const addAverage = (length: MovingAverageLength, color: string) => {
      if (!visibleAverages[length]) return;
      const series = chart.addSeries(LineSeries, { color, lineWidth: 2, lastValueVisible: false, priceLineVisible: false });
      series.setData(bars.flatMap((bar) => {
        const value = historyAverages.get(bar.tradeDate)?.[`ma${length}`];
        return value === null || value === undefined ? [] : [{ time: bar.tradeDate as Time, value }];
      }));
    };
    addAverage(5, "#10cae6"); addAverage(10, "#ffb317"); addAverage(20, "#ba94ff");

    const resetViewport = () => {
      const pointCount = visibleCount === null ? bars.length : Math.min(visibleCount, bars.length);
      chart.timeScale().setVisibleLogicalRange({
        from: Math.max(0, bars.length - pointCount),
        to: Math.max(0, bars.length - 1)
      });
    };
    const resizeChart = () => {
      const width = Math.floor(container.clientWidth);
      const height = Math.floor(container.clientHeight);
      if (width <= 0 || height <= 0) return false;
      chart.resize(width, height);
      return true;
    };

    // React can mount the canvas before the surrounding grid reaches its final
    // width.  Apply the requested logical range after the first measurable
    // layout so a short series cannot remain stranded in a narrow initial view.
    let viewportInitialized = false;
    const initializeViewport = () => {
      if (viewportInitialized || !resizeChart()) return;
      resetViewport();
      viewportInitialized = true;
    };
    const animationFrame = window.requestAnimationFrame(initializeViewport);
    const observer = new ResizeObserver(() => {
      resizeChart();
      initializeViewport();
    });
    observer.observe(container);
    return () => {
      window.cancelAnimationFrame(animationFrame);
      observer.disconnect();
      chart.remove();
    };
  }, [bars, historyBars, visibleAverages, visibleCount]);

  return <div ref={containerRef} className="etf-kline" aria-label="可缩放、可拖拽的 ETF K 线图" />;
}

export function EtfDetailPage() {
  const { tsCode = "" } = useParams();
  const { logout } = useAuth();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = (searchParams.get("tab") === "daily" || searchParams.get("tab") === "factors" ? searchParams.get("tab") : "basic") as DetailTab;
  const [etf, setEtf] = useState<EtfCode | null>(null);
  const [dailyBars, setDailyBars] = useState<EtfDailyBar[]>([]);
  const [factors, setFactors] = useState<EtfAdjustmentFactor[]>([]);
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [period, setPeriod] = useState<ChartPeriod>("day");
  const [adjustment, setAdjustment] = useState<AdjustmentMode>("raw");
  const [visibleCount, setVisibleCount] = useState<number | null>(200);
  const [visibleAverages, setVisibleAverages] = useState<Record<MovingAverageLength, boolean>>({ 5: true, 10: true, 20: true });
  const [loading, setLoading] = useState(true);
  const [seriesLoading, setSeriesLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const chartShellRef = useRef<HTMLElement>(null);
  const [isChartFullscreen, setIsChartFullscreen] = useState(false);

  const handleError = useCallback((caught: unknown, fallback: string) => {
    if (caught instanceof DataCollectionApiError && caught.status === 401) {
      logout(); navigate("/login", { replace: true }); return;
    }
    setError(caught instanceof Error ? caught.message : fallback);
  }, [logout, navigate]);

  useEffect(() => {
    let active = true;
    setLoading(true); setError(null);
    void getEtf(tsCode).then((value) => { if (active) setEtf(value); }).catch((caught) => { if (active) handleError(caught, "ETF 详情加载失败。"); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [handleError, tsCode]);

  const loadSeries = useCallback(async () => {
    setSeriesLoading(true); setError(null);
    try {
      const [nextBars, nextFactors] = await Promise.all([
        listEtfDailyBars(tsCode, {}),
        listEtfAdjustmentFactors(tsCode, {})
      ]);
      setDailyBars(nextBars); setFactors(nextFactors);
    } catch (caught) {
      handleError(caught, "ETF 时序数据加载失败。");
    } finally {
      setSeriesLoading(false);
    }
  }, [handleError, tsCode]);

  useEffect(() => { if (tab !== "basic") void loadSeries(); }, [loadSeries, tab]);

  useEffect(() => {
    const syncFullscreenState = () => setIsChartFullscreen(document.fullscreenElement === chartShellRef.current);
    document.addEventListener("fullscreenchange", syncFullscreenState);
    return () => document.removeEventListener("fullscreenchange", syncFullscreenState);
  }, []);

  const fullChartBars = useMemo(() => {
    const adjusted = adjustedBars(dailyBars, factors, adjustment);
    return adjusted === null ? null : aggregateBars(adjusted, period);
  }, [adjustment, dailyBars, factors, period]);
  const hasInvalidDateRange = Boolean(startDate && endDate && startDate > endDate);
  const chartBars = useMemo(() => {
    if (hasInvalidDateRange) return [];
    return fullChartBars?.filter((bar) => withinDateRange(bar.tradeDate, startDate, endDate)) ?? null;
  }, [endDate, fullChartBars, hasInvalidDateRange, startDate]);
  const visibleFactors = useMemo(
    () => factors.filter((factor) => withinDateRange(factor.trade_date, startDate, endDate)),
    [endDate, factors, startDate]
  );
  const availableAverages: Record<MovingAverageLength, boolean> = {
    5: (fullChartBars?.length ?? 0) >= 5,
    10: (fullChartBars?.length ?? 0) >= 10,
    20: (fullChartBars?.length ?? 0) >= 20
  };
  const displayName = etf?.csname ?? etf?.extname ?? etf?.ts_code ?? tsCode;
  const toggleChartFullscreen = useCallback(async () => {
    const chartShell = chartShellRef.current;
    if (!chartShell) return;
    try {
      if (document.fullscreenElement === chartShell) {
        await document.exitFullscreen();
      } else {
        await chartShell.requestFullscreen();
      }
    } catch {
      setError("无法切换 K 线图全屏显示，请检查浏览器权限后重试。");
    }
  }, []);

  if (loading) return <section className="collection-page"><div className="collection-empty-state">正在加载 ETF 详情…</div></section>;
  if (!etf) return <section className="collection-page"><div className="collection-empty-state">{error ?? "ETF 不存在。"}</div></section>;

  return <section className="collection-page etf-detail-page" aria-labelledby="etf-detail-title">
    <button className="etf-detail__back" type="button" onClick={() => navigate("/admin/data/etf-basics") }><ChevronLeft aria-hidden="true" />返回 ETF 列表</button>
    <div className="page-heading etf-detail__heading"><div><div className="etf-detail__title"><h2 id="etf-detail-title">{displayName}</h2><span>{etf.ts_code}</span></div><p>查看该 ETF 的基础资料、日线行情与复权因子</p></div></div>
    {error && <div className="page-error" role="alert">{error}</div>}
    <div className="etf-detail__tabs" role="tablist" aria-label="ETF 详情页签">{TABS.map((item) => <button key={item.key} className={tab === item.key ? "active" : ""} type="button" role="tab" aria-selected={tab === item.key} onClick={() => setSearchParams(item.key === "basic" ? {} : { tab: item.key })}>{item.label}</button>)}</div>
    {tab === "basic" && <section className="collection-table etf-detail__basic"><div className="collection-table__heading"><div><h3>基础资料</h3><span>ETF 基础信息的当前数据</span></div></div><dl><div><dt>交易所</dt><dd>{exchangeLabel(etf.exchange)}</dd></div><div><dt>上市状态</dt><dd>{statusLabel(etf.list_status)}</dd></div><div><dt>上市日期</dt><dd>{formatDate(etf.list_date)}</dd></div><div><dt>跟踪指数</dt><dd>{etf.index_name ?? "—"}</dd></div><div><dt>管理人</dt><dd>{etf.mgr_name ?? "—"}</dd></div><div><dt>管理费率</dt><dd>{etf.mgt_fee ? `${numberText(etf.mgt_fee)}%` : "—"}</dd></div></dl></section>}
    {tab !== "basic" && <>
      <div className="etf-detail__filters">
        <label className="etf-detail__filter-field etf-detail__filter-field--start">
          <span>开始日期</span>
          <input type="date" value={startDate} aria-describedby="etf-detail-date-filter-help" aria-invalid={hasInvalidDateRange} onChange={(event) => setStartDate(event.target.value)} />
        </label>
        <label className="etf-detail__filter-field etf-detail__filter-field--end">
          <span>结束日期</span>
          <input type="date" value={endDate} aria-describedby="etf-detail-date-filter-help" aria-invalid={hasInvalidDateRange} onChange={(event) => setEndDate(event.target.value)} />
        </label>
        <button className="toolbar-button" type="button" title="重新请求完整历史数据；日期筛选会立即作用于当前视图。" disabled={seriesLoading || hasInvalidDateRange} onClick={() => void loadSeries()}><RefreshCw className={seriesLoading ? "spin" : ""} aria-hidden="true" />重新加载数据</button>
        <div className="etf-detail__filter-feedback" id="etf-detail-date-filter-help">
          <span>日期筛选即时生效；留空表示全部日线数据。</span>
          {hasInvalidDateRange && <strong role="alert">开始日期不能晚于结束日期。</strong>}
        </div>
      </div>
      {tab === "daily" && <section ref={chartShellRef} className="etf-detail__chart-shell">
        <div className="etf-detail__chart-heading">
          <h3>K 线</h3>
          <button className="etf-detail__expand" type="button" aria-label={isChartFullscreen ? "退出 K 线图全屏" : "全屏显示 K 线图"} onClick={() => void toggleChartFullscreen()}><Expand aria-hidden="true" />{isChartFullscreen ? "退出全屏" : "全屏"}</button>
        </div>
        <div className="etf-detail__chart-controls">
          <div>{(["day", "week", "month", "year"] as ChartPeriod[]).map((item) => <button key={item} className={period === item ? "active" : ""} type="button" onClick={() => setPeriod(item)}>{PERIOD_LABELS[item]}</button>)}</div>
          <div className="etf-detail__ma-toggles">{MOVING_AVERAGE_LENGTHS.map((length) => <button key={length} type="button" aria-pressed={availableAverages[length] && visibleAverages[length]} disabled={!availableAverages[length]} title={availableAverages[length] ? `显示或隐藏 MA${length}` : `至少需要 ${length} 根${PERIOD_KLINE_LABELS[period]}数据`} onClick={() => setVisibleAverages((current) => ({ ...current, [length]: !current[length] }))}><i className={`ma${length}`} aria-hidden="true" />MA{length}</button>)}</div>
          <div>{(["raw", "forward", "backward"] as AdjustmentMode[]).map((item) => <button key={item} className={adjustment === item ? "active" : ""} type="button" onClick={() => setAdjustment(item)}>{({ raw: "不复权", forward: "前复权", backward: "后复权" })[item]}</button>)}</div>
        </div>
        <div className="etf-detail__range-controls">{([30, 60, 90, 200, null] as (number | null)[]).map((count) => <button key={count ?? "all"} className={visibleCount === count ? "active" : ""} type="button" onClick={() => setVisibleCount(count)}>{count === null ? "全部" : `${count} ${PERIOD_LABELS[period]}`}</button>)}<span>滚轮缩放，拖拽回看</span></div>
        {hasInvalidDateRange ? <div className="etf-detail__chart-empty">开始日期不能晚于结束日期。</div> : seriesLoading ? <div className="etf-detail__chart-empty">正在加载日线数据…</div> : chartBars === null || fullChartBars === null ? <div className="etf-detail__chart-empty">所选区间的复权因子不完整，无法生成复权 K 线。</div> : chartBars.length === 0 ? <div className="etf-detail__chart-empty">该日期范围内没有日线数据。</div> : <InteractiveKLineChart bars={chartBars} historyBars={fullChartBars} visibleCount={visibleCount} visibleAverages={visibleAverages} />}
      </section>}
      {tab === "factors" && <section className="collection-table etf-detail__factor-table"><div className="collection-table__heading"><div><h3>复权因子</h3><span>按交易日期升序展示</span></div><strong>{seriesLoading ? "正在加载…" : `${visibleFactors.length.toLocaleString("zh-CN")} 条`}</strong></div><div className="collection-table__scroll"><table><thead><tr><th>交易日期</th><th>复权因子</th><th>数据来源</th><th>更新时间</th></tr></thead><tbody>{seriesLoading ? <tr><td colSpan={4} className="collection-table__empty">正在加载复权因子…</td></tr> : visibleFactors.length === 0 ? <tr><td colSpan={4} className="collection-table__empty">该日期范围内没有复权因子数据</td></tr> : visibleFactors.map((factor) => <tr key={factor.trade_date}><td>{formatDate(factor.trade_date)}</td><td>{numberText(factor.adj_factor, 12)}</td><td>{factor.source}</td><td>{formatTimestamp(factor.updated_at)}</td></tr>)}</tbody></table></div></section>}
    </>}
  </section>;
}
