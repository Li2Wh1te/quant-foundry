import { ChevronLeft, Expand, RefreshCw } from "lucide-react";
import {
  dispose as disposeKLineChart,
  init as initKLineChart,
  registerIndicator,
  type IndicatorTemplate,
  type KLineData
} from "klinecharts";
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
type KLineChartPeriod = "day" | "week" | "month" | "year";

const KLINE_MA_INDICATOR_NAME = "ETF_MA";
const KLINE_MA_COLORS: Record<MovingAverageLength, string> = {
  5: "#10cae6",
  10: "#ffb317",
  20: "#ba94ff"
};
const KLINE_UP_COLOR = "#f35b69";
const KLINE_DOWN_COLOR = "#45cc91";

interface KLineChartBar extends KLineData {
  ma5?: number;
  ma10?: number;
  ma20?: number;
}

interface KLineMovingAverageResult {
  [key: string]: number | undefined;
}

// KLineChart's built-in MA indicator starts its calculation at the first bar
// in the current data set.  This adapter renders values precomputed from the
// complete history, so a date-filtered view keeps the correct MA warm-up data.
const ETF_MA_INDICATOR = {
  name: KLINE_MA_INDICATOR_NAME,
  shortName: "MA",
  series: "price",
  precision: 4,
  calcParams: [...MOVING_AVERAGE_LENGTHS],
  figures: [],
  regenerateFigures: (params: number[]) => params.map((length) => ({
    key: `ma${length}`,
    title: `MA${length}: `,
    type: "line"
  })),
  calc: (dataList: KLineData[], indicator: { calcParams: number[] }): KLineMovingAverageResult[] => dataList.map((bar) => indicator.calcParams.reduce<KLineMovingAverageResult>((result, length) => {
    const value = bar[`ma${length}`];
    if (typeof value === "number") result[`ma${length}`] = value;
    return result;
  }, {}))
} as unknown as IndicatorTemplate<KLineMovingAverageResult, number, unknown>;

registerIndicator(ETF_MA_INDICATOR);

interface NumericBar {
  tradeDate: string;
  open: number;
  high: number;
  low: number;
  close: number;
  vol: number;
  amount: number;
}

interface MatchedAdjustmentBar {
  bar: NumericBar;
  factor: number;
}

interface AdjustmentFactorSummary {
  first: number;
  last: number;
  changeCount: number;
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

const ADJUSTMENT_LABELS: Record<AdjustmentMode, string> = {
  raw: "不复权",
  forward: "前复权",
  backward: "后复权"
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

function toNumericBarList(sourceBars: EtfDailyBar[]): NumericBar[] {
  return sourceBars.map(toNumericBars).filter((bar): bar is NumericBar => bar !== null);
}

function matchAdjustmentFactors(
  rawBars: NumericBar[],
  factors: EtfAdjustmentFactor[]
): MatchedAdjustmentBar[] | null {
  // A valid cumulative factor is required for every plotted trading session.
  // Falling back to an unadjusted price would silently mix price bases.
  const factorByDate = new Map(factors.map((factor) => [factor.trade_date, decimal(factor.adj_factor)]));
  const matched = rawBars.map((bar) => ({ bar, factor: factorByDate.get(bar.tradeDate) }));
  if (matched.some(({ factor }) => factor === null || factor === undefined || factor <= 0)) return null;
  return matched.map(({ bar, factor }) => ({ bar, factor: factor! }));
}

function adjustedBars(
  sourceBars: EtfDailyBar[],
  factors: EtfAdjustmentFactor[],
  adjustment: AdjustmentMode
): NumericBar[] | null {
  const rawBars = toNumericBarList(sourceBars);
  if (adjustment === "raw") return rawBars;

  // Tushare factors are normalized against the selected range endpoint so that
  // forward-adjusted prices end at the raw latest price and backward-adjusted
  // prices start at the raw earliest price.
  const matched = matchAdjustmentFactors(rawBars, factors);
  if (matched === null) return null;

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

function summarizeAdjustmentFactors(
  sourceBars: EtfDailyBar[],
  factors: EtfAdjustmentFactor[]
): AdjustmentFactorSummary | null {
  const matched = matchAdjustmentFactors(toNumericBarList(sourceBars), factors);
  if (matched === null || matched.length === 0) return null;

  let changeCount = 0;
  for (let index = 1; index < matched.length; index += 1) {
    if (matched[index]!.factor !== matched[index - 1]!.factor) changeCount += 1;
  }
  return {
    first: matched[0]!.factor,
    last: matched.at(-1)!.factor,
    changeCount
  };
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

function kLineTimestamp(tradeDate: string): number {
  const timestamp = Date.parse(`${tradeDate}T00:00:00Z`);
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function chartNumber(value: number, fractionDigits = 4): string {
  return value.toLocaleString("zh-CN", { maximumFractionDigits: fractionDigits });
}

function chartVolume(value: number): string {
  const absolute = Math.abs(value);
  if (absolute >= 100_000_000) return `${(value / 100_000_000).toFixed(2)} 亿`;
  if (absolute >= 10_000) return `${(value / 10_000).toFixed(2)} 万`;
  return value.toLocaleString("zh-CN", { maximumFractionDigits: 0 });
}

function adjustmentFactorText(value: number): string {
  return value.toLocaleString("zh-CN", { maximumFractionDigits: 12 });
}

function adjustmentNotice(
  adjustment: AdjustmentMode,
  summary: AdjustmentFactorSummary | null
): string | null {
  if (adjustment === "raw" || summary === null) return null;
  const label = ADJUSTMENT_LABELS[adjustment];
  if (summary.changeCount === 0) {
    return `${label}：当前日期筛选范围内复权因子未变化，K 线价格与不复权一致。`;
  }
  return `${label}：已按复权因子调整（${adjustmentFactorText(summary.first)} → ${adjustmentFactorText(summary.last)}，共 ${summary.changeCount} 次变动）。`;
}

const KLINE_PERIOD_TYPES: Record<ChartPeriod, KLineChartPeriod> = {
  day: "day",
  week: "week",
  month: "month",
  year: "year"
};

const KLINE_CHART_OPTIONS = {
  locale: "zh-CN",
  timezone: "Asia/Shanghai",
  zoomAnchor: "cursor" as const,
  layout: {
    barSpaceLimit: { min: 1, max: 50 },
    pane: { minHeight: 30, dragEnabled: false },
    yAxis: {
      position: "right" as const,
      inside: false,
      scrollZoomEnabled: false,
      gap: { top: 0.16, bottom: 0.12 }
    }
  },
  styles: {
    grid: {
      show: true,
      horizontal: { show: true, style: "solid" as const, size: 1, color: "#343940" },
      vertical: { show: true, style: "solid" as const, size: 1, color: "#2b3037" }
    },
    candle: {
      type: "candle_solid" as const,
      bar: {
        compareRule: "current_open" as const,
        upColor: KLINE_UP_COLOR,
        downColor: KLINE_DOWN_COLOR,
        noChangeColor: "#8c96a6",
        upBorderColor: KLINE_UP_COLOR,
        downBorderColor: KLINE_DOWN_COLOR,
        noChangeBorderColor: "#8c96a6",
        upWickColor: KLINE_UP_COLOR,
        downWickColor: KLINE_DOWN_COLOR,
        noChangeWickColor: "#8c96a6"
      },
      priceMark: {
        show: true,
        high: { show: false },
        low: { show: false },
        // The latest price is presented in the compact summary above the plot.
        last: { show: false }
      },
      tooltip: { showRule: "follow_cross" as const, showType: "standard" as const }
    },
    indicator: {
      bars: [{
        style: "fill" as const,
        borderSize: 0,
        upColor: "rgba(243, 91, 105, 0.72)",
        downColor: "rgba(69, 204, 145, 0.72)",
        noChangeColor: "rgba(140, 150, 166, 0.72)"
      }],
      lines: MOVING_AVERAGE_LENGTHS.map((length) => ({ style: "solid" as const, smooth: false, size: 2, color: KLINE_MA_COLORS[length] })),
      lastValueMark: { show: false },
      tooltip: { showRule: "follow_cross" as const }
    },
    xAxis: {
      show: true,
      size: 28,
      axisLine: { show: true, color: "#343940", size: 1 },
      tickLine: { show: false },
      tickText: { show: true, color: "#aab4c4", size: 11, marginStart: 4, marginEnd: 4 }
    },
    yAxis: {
      show: true,
      size: 64,
      axisLine: { show: true, color: "#343940", size: 1 },
      tickLine: { show: false },
      tickText: { show: true, color: "#aab4c4", size: 11, marginStart: 4, marginEnd: 5 }
    },
    separator: { size: 1, color: "#343940", fill: true, activeBackgroundColor: "rgba(52, 57, 64, 0.2)" },
    crosshair: {
      show: true,
      horizontal: {
        show: true,
        line: { show: true, style: "dashed" as const, dashedValue: [4, 2], size: 1, color: "#718096" },
        text: { show: true, color: "#ffffff", size: 11, backgroundColor: "#343940", borderColor: "#343940", borderSize: 0, borderRadius: 3 }
      },
      vertical: {
        show: true,
        line: { show: true, style: "dashed" as const, dashedValue: [4, 2], size: 1, color: "#718096" },
        text: { show: true, color: "#ffffff", size: 11, backgroundColor: "#343940", borderColor: "#343940", borderSize: 0, borderRadius: 3 }
      }
    }
  }
};

function InteractiveKLineChart({
  bars,
  historyBars,
  period,
  visibleCount,
  visibleAverages,
  adjustment
}: {
  bars: NumericBar[];
  historyBars: NumericBar[];
  period: ChartPeriod;
  visibleCount: number | null;
  visibleAverages: Record<MovingAverageLength, boolean>;
  adjustment: AdjustmentMode;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const orderedBars = useMemo(() => [...bars].sort((left, right) => left.tradeDate.localeCompare(right.tradeDate)), [bars]);
  const orderedHistoryBars = useMemo(() => [...historyBars].sort((left, right) => left.tradeDate.localeCompare(right.tradeDate)), [historyBars]);
  const latestBar = orderedBars.at(-1);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || orderedBars.length === 0) return;

    const historyAverages = new Map<string, Record<string, number | null>>();
    for (const length of MOVING_AVERAGE_LENGTHS) {
      const values = movingAverage(orderedHistoryBars, length);
      values.forEach((value, index) => {
        const date = orderedHistoryBars[index]?.tradeDate;
        if (!date) return;
        historyAverages.set(date, { ...(historyAverages.get(date) ?? {}), [`ma${length}`]: value });
      });
    }
    const chartData: KLineChartBar[] = orderedBars.map((bar) => ({
      timestamp: kLineTimestamp(bar.tradeDate),
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
      volume: bar.vol,
      turnover: bar.amount,
      ...(historyAverages.get(bar.tradeDate) ?? {})
    }));

    const chart = initKLineChart(container, KLINE_CHART_OPTIONS);
    if (!chart) return;
    // KLineChart otherwise keeps two bars visible at either edge, allowing a
    // pan gesture to leave most of the viewport empty. Zero-distance limits
    // keep every draggable position inside the available market data.
    chart.setMaxOffsetLeftDistance(0);
    chart.setMaxOffsetRightDistance(0);
    chart.setOffsetRightDistance(0);
    // KLineChart normally interprets a drag on the x-axis strip as a zoom
    // gesture.  That makes a horizontal pan change behaviour near the chart
    // boundary, so keep one predictable rule: dragging reviews history and
    // the mouse wheel is the only direct zoom control.
    chart.overrideXAxis({ scrollZoomEnabled: false });
    chart.setSymbol({ ticker: "ETF", pricePrecision: 4, volumePrecision: 0 });
    chart.setPeriod({ span: 1, type: KLINE_PERIOD_TYPES[period] });
    chart.setDataLoader({
      getBars: ({ callback }) => callback(chartData, false)
    });

    const activeAverages = MOVING_AVERAGE_LENGTHS.filter((length) => visibleAverages[length] && orderedHistoryBars.length >= length);
    if (activeAverages.length > 0) {
      chart.createIndicator({
        name: KLINE_MA_INDICATOR_NAME,
        paneId: "candle_pane",
        calcParams: activeAverages,
        styles: { lines: activeAverages.map((length) => ({ color: KLINE_MA_COLORS[length], size: 2 })) }
      }, true);
    }
    const volumePaneId = chart.createIndicator({
      name: "VOL",
      calcParams: [],
      styles: { lastValueMark: { show: false } }
    }, false);
    if (volumePaneId) chart.setPaneOptions({ id: volumePaneId, height: 92, minHeight: 76, dragEnabled: false });
    // Apply the selected density after the first measurable layout, then align
    // the newest bar to the data boundary without introducing blank space.
    let viewportInitialized = false;
    const initializeViewport = () => {
      if (viewportInitialized || container.clientWidth <= 0) return;
      const plotWidth = Math.max(container.clientWidth - 90, 1);
      const targetBarCount = visibleCount === null ? orderedBars.length : Math.min(visibleCount, orderedBars.length);
      const barSpace = Math.min(50, Math.max(1, plotWidth / Math.max(targetBarCount, 1)));
      chart.setBarSpace(barSpace);
      chart.setOffsetRightDistance(0);
      chart.scrollToRealTime();
      viewportInitialized = true;
    };
    const animationFrame = window.requestAnimationFrame(initializeViewport);
    const observer = new ResizeObserver(() => {
      initializeViewport();
    });
    observer.observe(container);
    return () => {
      window.cancelAnimationFrame(animationFrame);
      observer.disconnect();
      disposeKLineChart(chart);
    };
  }, [orderedBars, orderedHistoryBars, period, visibleAverages, visibleCount]);

  if (!latestBar) return null;
  const priceDirection = latestBar.close >= latestBar.open ? "up" : "down";
  const closeLabel = adjustment === "raw" ? "收盘" : `${ADJUSTMENT_LABELS[adjustment]}收盘`;
  return <div className="etf-kline-wrap">
    <div className="etf-kline__summary" aria-label="最新行情">
      <span className="etf-kline__summary-date">{formatDate(latestBar.tradeDate)}</span>
      <span className="etf-kline__summary-item"><span>{closeLabel}</span><strong className={`etf-kline__summary-value etf-kline__summary-value--${priceDirection}`}>{chartNumber(latestBar.close)}</strong></span>
      <span className="etf-kline__summary-item"><span>成交量</span><strong>{chartVolume(latestBar.vol)}</strong></span>
    </div>
    <div ref={containerRef} className="etf-kline" aria-label="可缩放、可在数据范围内横向拖拽回看的 ETF K 线图" />
  </div>;
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

  const hasInvalidDateRange = Boolean(startDate && endDate && startDate > endDate);
  const selectedDailyBars = useMemo(
    () => hasInvalidDateRange ? [] : dailyBars.filter((bar) => withinDateRange(bar.trade_date, startDate, endDate)),
    [dailyBars, endDate, hasInvalidDateRange, startDate]
  );
  const fullChartBars = useMemo(() => {
    const adjusted = adjustedBars(dailyBars, factors, adjustment);
    return adjusted === null ? null : aggregateBars(adjusted, period);
  }, [adjustment, dailyBars, factors, period]);
  const chartBars = useMemo(() => {
    if (hasInvalidDateRange) return [];
    return fullChartBars?.filter((bar) => withinDateRange(bar.tradeDate, startDate, endDate)) ?? null;
  }, [endDate, fullChartBars, hasInvalidDateRange, startDate]);
  const adjustmentSummary = useMemo(
    () => adjustment === "raw" ? null : summarizeAdjustmentFactors(selectedDailyBars, factors),
    [adjustment, factors, selectedDailyBars]
  );
  const currentAdjustmentNotice = adjustmentNotice(adjustment, adjustmentSummary);
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
          <div>{(["raw", "forward", "backward"] as AdjustmentMode[]).map((item) => <button key={item} className={adjustment === item ? "active" : ""} type="button" onClick={() => setAdjustment(item)}>{ADJUSTMENT_LABELS[item]}</button>)}</div>
        </div>
        <div className="etf-detail__range-controls">{([30, 60, 90, 200, null] as (number | null)[]).map((count) => <button key={count ?? "all"} className={visibleCount === count ? "active" : ""} type="button" onClick={() => setVisibleCount(count)}>{count === null ? "全部" : `${count} ${PERIOD_LABELS[period]}`}</button>)}<span>滚轮缩放；图内横向拖拽回看</span></div>
        {currentAdjustmentNotice && <p className="etf-detail__adjustment-note" role="status" aria-live="polite">{currentAdjustmentNotice}</p>}
        {hasInvalidDateRange ? <div className="etf-detail__chart-empty">开始日期不能晚于结束日期。</div> : seriesLoading ? <div className="etf-detail__chart-empty">正在加载日线数据…</div> : chartBars === null || fullChartBars === null ? <div className="etf-detail__chart-empty">所选区间的复权因子不完整，无法生成复权 K 线。</div> : chartBars.length === 0 ? <div className="etf-detail__chart-empty">该日期范围内没有日线数据。</div> : <InteractiveKLineChart bars={chartBars} historyBars={fullChartBars} period={period} visibleCount={visibleCount} visibleAverages={visibleAverages} adjustment={adjustment} />}
      </section>}
      {tab === "factors" && <section className="collection-table etf-detail__factor-table"><div className="collection-table__heading"><div><h3>复权因子</h3><span>按交易日期升序展示</span></div><strong>{seriesLoading ? "正在加载…" : `${visibleFactors.length.toLocaleString("zh-CN")} 条`}</strong></div><div className="collection-table__scroll"><table><thead><tr><th>交易日期</th><th>复权因子</th><th>数据来源</th><th>更新时间</th></tr></thead><tbody>{seriesLoading ? <tr><td colSpan={4} className="collection-table__empty">正在加载复权因子…</td></tr> : visibleFactors.length === 0 ? <tr><td colSpan={4} className="collection-table__empty">该日期范围内没有复权因子数据</td></tr> : visibleFactors.map((factor) => <tr key={factor.trade_date}><td>{formatDate(factor.trade_date)}</td><td>{numberText(factor.adj_factor, 12)}</td><td>{factor.source}</td><td>{formatTimestamp(factor.updated_at)}</td></tr>)}</tbody></table></div></section>}
    </>}
  </section>;
}
