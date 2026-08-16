import { ChevronLeft, Expand, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
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

function isoDate(value: Date): string {
  return value.toISOString().slice(0, 10);
}

function initialStartDate(): string {
  const value = new Date();
  value.setFullYear(value.getFullYear() - 1);
  return isoDate(value);
}

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

function movingAverage(bars: NumericBar[], length: number): (number | null)[] {
  return bars.map((_, index) => {
    if (index + 1 < length) return null;
    const closes = bars.slice(index + 1 - length, index + 1).map((bar) => bar.close);
    return closes.reduce((sum, value) => sum + value, 0) / length;
  });
}

function linePath(values: (number | null)[], x: (index: number) => number, y: (value: number) => number): string {
  let path = "";
  let pendingMove = true;
  values.forEach((value, index) => {
    if (value === null) { pendingMove = true; return; }
    path += `${pendingMove ? "M" : "L"}${x(index).toFixed(2)},${y(value).toFixed(2)} `;
    pendingMove = false;
  });
  return path;
}

function KLineChart({ bars }: { bars: NumericBar[] }) {
  // A viewBox keeps the chart readable at every container width while all
  // coordinates remain derived from the currently loaded series.
  const chart = useMemo(() => {
    const width = 920, height = 390, left = 48, right = 70, top = 20, priceBottom = 276, volumeTop = 294, bottom = 342;
    const prices = bars.flatMap((bar) => [bar.high, bar.low]);
    const min = Math.min(...prices), max = Math.max(...prices), padding = Math.max((max - min) * 0.08, max * 0.001);
    const floor = min - padding, ceiling = max + padding, innerWidth = width - left - right;
    const x = (index: number) => left + ((index + 0.5) / bars.length) * innerWidth;
    const y = (value: number) => top + ((ceiling - value) / (ceiling - floor)) * (priceBottom - top);
    const maxVolume = Math.max(...bars.map((bar) => bar.vol), 1);
    const volumeY = (value: number) => bottom - (value / maxVolume) * (bottom - volumeTop);
    return { width, height, left, right, top, priceBottom, volumeTop, bottom, floor, ceiling, x, y, volumeY, candleWidth: Math.max(2, Math.min(12, innerWidth / bars.length * 0.62)) };
  }, [bars]);
  const ma5 = useMemo(() => movingAverage(bars, 5), [bars]);
  const ma10 = useMemo(() => movingAverage(bars, 10), [bars]);
  const ma20 = useMemo(() => movingAverage(bars, 20), [bars]);
  const priceTicks = Array.from({ length: 5 }, (_, index) => chart.floor + ((chart.ceiling - chart.floor) * index / 4));
  const dateTicks = bars.length <= 5 ? bars.map((_, index) => index) : [0, Math.floor((bars.length - 1) / 3), Math.floor((bars.length - 1) * 2 / 3), bars.length - 1];

  return <svg className="etf-kline" viewBox={`0 0 ${chart.width} ${chart.height}`} role="img" aria-label="ETF K 线图，包含成交量和移动平均线">
    {priceTicks.map((value) => <g key={value}><line className="etf-kline__grid" x1={chart.left} x2={chart.width - chart.right} y1={chart.y(value)} y2={chart.y(value)} /><text className="etf-kline__axis" x={chart.width - chart.right + 10} y={chart.y(value) + 4}>{value.toFixed(3)}</text></g>)}
    <line className="etf-kline__grid" x1={chart.left} x2={chart.width - chart.right} y1={chart.volumeTop} y2={chart.volumeTop} />
    {bars.map((bar, index) => {
      const rising = bar.close >= bar.open;
      const className = rising ? "etf-kline__up" : "etf-kline__down";
      const x = chart.x(index), bodyTop = Math.min(chart.y(bar.open), chart.y(bar.close)), bodyBottom = Math.max(chart.y(bar.open), chart.y(bar.close));
      return <g key={bar.tradeDate} className={className}><line x1={x} x2={x} y1={chart.y(bar.high)} y2={chart.y(bar.low)} /><rect x={x - chart.candleWidth / 2} y={bodyTop} width={chart.candleWidth} height={Math.max(1.5, bodyBottom - bodyTop)} /><rect x={x - chart.candleWidth / 2} y={chart.volumeY(bar.vol)} width={chart.candleWidth} height={chart.bottom - chart.volumeY(bar.vol)} opacity="0.72" /></g>;
    })}
    <path className="etf-kline__ma etf-kline__ma--5" d={linePath(ma5, chart.x, chart.y)} /><path className="etf-kline__ma etf-kline__ma--10" d={linePath(ma10, chart.x, chart.y)} /><path className="etf-kline__ma etf-kline__ma--20" d={linePath(ma20, chart.x, chart.y)} />
    {dateTicks.map((index) => <text key={index} className="etf-kline__axis" textAnchor="middle" x={chart.x(index)} y={chart.height - 14}>{formatDate(bars[index].tradeDate).slice(5)}</text>)}
  </svg>;
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
  const [startDate, setStartDate] = useState(initialStartDate);
  const [endDate, setEndDate] = useState(() => isoDate(new Date()));
  const [period, setPeriod] = useState<ChartPeriod>("day");
  const [adjustment, setAdjustment] = useState<AdjustmentMode>("raw");
  const [loading, setLoading] = useState(true);
  const [seriesLoading, setSeriesLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
    if (startDate > endDate) { setError("开始日期不能晚于结束日期。"); return; }
    setSeriesLoading(true); setError(null);
    try {
      const [nextBars, nextFactors] = await Promise.all([
        listEtfDailyBars(tsCode, { startDate, endDate, limit: 5_000 }),
        listEtfAdjustmentFactors(tsCode, { startDate, endDate, limit: 5_000 })
      ]);
      setDailyBars(nextBars); setFactors(nextFactors);
    } catch (caught) {
      handleError(caught, "ETF 时序数据加载失败。");
    } finally {
      setSeriesLoading(false);
    }
  }, [endDate, handleError, startDate, tsCode]);

  useEffect(() => { if (tab !== "basic") void loadSeries(); }, [loadSeries, tab]);

  const chartBars = useMemo(() => {
    const adjusted = adjustedBars(dailyBars, factors, adjustment);
    return adjusted === null ? null : aggregateBars(adjusted, period);
  }, [adjustment, dailyBars, factors, period]);
  const displayName = etf?.csname ?? etf?.extname ?? etf?.ts_code ?? tsCode;

  if (loading) return <section className="collection-page"><div className="collection-empty-state">正在加载 ETF 详情…</div></section>;
  if (!etf) return <section className="collection-page"><div className="collection-empty-state">{error ?? "ETF 不存在。"}</div></section>;

  return <section className="collection-page etf-detail-page" aria-labelledby="etf-detail-title">
    <button className="etf-detail__back" type="button" onClick={() => navigate("/admin/data/etf-basics") }><ChevronLeft aria-hidden="true" />返回 ETF 列表</button>
    <div className="page-heading etf-detail__heading"><div><div className="etf-detail__title"><h2 id="etf-detail-title">{displayName}</h2><span>{etf.ts_code}</span></div><p>查看该 ETF 的基础资料、日线行情与复权因子</p></div></div>
    {error && <div className="page-error" role="alert">{error}</div>}
    <div className="etf-detail__tabs" role="tablist" aria-label="ETF 详情页签">{TABS.map((item) => <button key={item.key} className={tab === item.key ? "active" : ""} type="button" role="tab" aria-selected={tab === item.key} onClick={() => setSearchParams(item.key === "basic" ? {} : { tab: item.key })}>{item.label}</button>)}</div>
    {tab === "basic" && <section className="collection-table etf-detail__basic"><div className="collection-table__heading"><div><h3>基础资料</h3><span>ETF 基础信息的当前数据</span></div></div><dl><div><dt>交易所</dt><dd>{exchangeLabel(etf.exchange)}</dd></div><div><dt>上市状态</dt><dd>{statusLabel(etf.list_status)}</dd></div><div><dt>上市日期</dt><dd>{formatDate(etf.list_date)}</dd></div><div><dt>跟踪指数</dt><dd>{etf.index_name ?? "—"}</dd></div><div><dt>管理人</dt><dd>{etf.mgr_name ?? "—"}</dd></div><div><dt>管理费率</dt><dd>{etf.mgt_fee ? `${numberText(etf.mgt_fee)}%` : "—"}</dd></div></dl></section>}
    {tab !== "basic" && <><div className="etf-detail__filters"><label>开始日期<input type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} /></label><label>结束日期<input type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} /></label><button className="toolbar-button" type="button" disabled={seriesLoading} onClick={() => void loadSeries()}><RefreshCw className={seriesLoading ? "spin" : ""} aria-hidden="true" />查询</button></div>{tab === "daily" && <section className="etf-detail__chart-shell"><div className="etf-detail__chart-heading"><h3>K 线</h3><button className="etf-detail__expand" type="button" onClick={() => document.querySelector(".etf-detail__chart-shell")?.requestFullscreen()}><Expand aria-hidden="true" />全屏</button></div><div className="etf-detail__chart-controls"><div>{(["day", "week", "month", "year"] as ChartPeriod[]).map((item) => <button key={item} className={period === item ? "active" : ""} type="button" onClick={() => setPeriod(item)}>{({ day: "日", week: "周", month: "月", year: "年" })[item]}</button>)}</div><span><i className="ma5" />MA5 <i className="ma10" />MA10 <i className="ma20" />MA20</span><div>{(["raw", "forward", "backward"] as AdjustmentMode[]).map((item) => <button key={item} className={adjustment === item ? "active" : ""} type="button" onClick={() => setAdjustment(item)}>{({ raw: "不复权", forward: "前复权", backward: "后复权" })[item]}</button>)}</div></div>{seriesLoading ? <div className="etf-detail__chart-empty">正在加载日线数据…</div> : chartBars === null ? <div className="etf-detail__chart-empty">所选区间的复权因子不完整，无法生成复权 K 线。</div> : chartBars.length === 0 ? <div className="etf-detail__chart-empty">该日期范围内没有日线数据。</div> : <KLineChart bars={chartBars} />}</section>}{tab === "factors" && <section className="collection-table etf-detail__factor-table"><div className="collection-table__heading"><div><h3>复权因子</h3><span>按交易日期升序展示</span></div><strong>{seriesLoading ? "正在加载…" : `${factors.length.toLocaleString("zh-CN")} 条`}</strong></div><div className="collection-table__scroll"><table><thead><tr><th>交易日期</th><th>复权因子</th><th>数据来源</th><th>更新时间</th></tr></thead><tbody>{seriesLoading ? <tr><td colSpan={4} className="collection-table__empty">正在加载复权因子…</td></tr> : factors.length === 0 ? <tr><td colSpan={4} className="collection-table__empty">该日期范围内没有复权因子数据</td></tr> : factors.map((factor) => <tr key={factor.trade_date}><td>{formatDate(factor.trade_date)}</td><td>{numberText(factor.adj_factor, 12)}</td><td>{factor.source}</td><td>{formatTimestamp(factor.updated_at)}</td></tr>)}</tbody></table></div></section>}</>}
  </section>;
}
