import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import {
  ArrowLeft,
  Bell,
  ChevronDown,
  Expand,
  Maximize,
  MousePointer2,
  PanelRight,
  Pause,
  Play,
  RefreshCw,
  Ruler,
  SkipForward,
  Slash,
  TextCursorInput,
  Trash2,
  X,
  ZoomIn,
  Minus,
  ChevronsUpDown,
  ChartCandlestick,
} from "lucide-react";
import { DataCollectionApiError } from "../api/dataCollections";
import { MarketApiError, exactMarketValue, type Adjustment as MarketAdjustment } from "../features/market-data/data";
import { useMarketInstrument } from "../features/market-data/catalog/MarketInstrumentContext";
import { useFormalDetail } from "../features/market-data/series/useFormalDetail";
import { plotBars, exactBarValue } from "../features/market-data/series/plotting";
import { matchesBarWindow, shanghaiBusinessDate } from "../features/market-data/series/window";
import type { DetailBasisSlots } from "../features/market-data/series/basisSlot";
import { BasisControls as DefaultBasisControls } from "../features/market-data/basis/BasisControls";
import { FactorTable as DefaultFactorTable } from "../features/market-data/basis/FactorTable";
import { useAuth } from "../auth/AuthContext";
import { marketReturnTo } from "./market/marketPresentation";
import {
  ADJUSTMENTS,
  dateText,
  DEFAULT_INDICATORS,
  exchangeName,
  MA_COLORS,
  PERIODS,
  timeText,
  validParams,
  type Adjustment,
  type Bar,
  type IndicatorSettings,
  type Period,
} from "./etf/etfData";
import { EtfChart, type ChartHandle } from "./etf/EtfChart";
import { EtfPopover, type PopoverAnchor } from "./etf/EtfPopover";
import { EtfWatchlist } from "./etf/EtfWatchlist";
import "./etf/EtfWorkspace.css";
import "../features/market-data/series/series.css";

const TOOL_LABELS: Record<string, string> = {
  cursor: "光标",
  segment: "趋势线",
  parallelStraightLine: "平行通道",
  fibonacciLine: "斐波那契",
  simpleAnnotation: "标注",
  qfRuler: "测量",
  zoom: "缩放",
};
const TOOL_HINTS: Record<string, string> = {
  segment: "点击两个锚点绘制趋势线",
  parallelStraightLine: "点击两个方向锚点，再点击通道边界",
  fibonacciLine: "点击起点和终点绘制斐波那契",
  simpleAnnotation: "点击图表放置标注",
  qfRuler: "点击两个锚点测量价差和时间",
};
function message(error: unknown) {
  return error instanceof Error ? error.message : "数据加载失败，请重试。";
}
function Kv({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="qfe-cell">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

export function EtfDetailPage({ BasisControls = DefaultBasisControls, FactorTable = DefaultFactorTable }: DetailBasisSlots = {}) {
  const entry = useMarketInstrument();
  const { tsCode = "" } = useParams(),
    navigate = useNavigate(),
    location = useLocation(),
    { logout } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const tab =
    searchParams.get("tab") === "factors"
      ? "factors"
      : searchParams.get("tab") === "overview"
        ? "overview"
        : "basic";
  const [refresh, setRefresh] = useState(0);
  const [seriesKey, setSeriesKey] = useState("etf-daily-local");
  const [fixedEnd, setFixedEnd] = useState(shanghaiBusinessDate);
  const [replaySteps, setReplaySteps] = useState<{ code: string; dates: string[] }>({ code: "", dates: [] });
  const [period, setPeriod] = useState<Period>("day"),
    [adjustment, setAdjustment] = useState<Adjustment>("raw"),
    [indicators, setIndicators] =
      useState<IndicatorSettings>(DEFAULT_INDICATORS);
  const [range, setRange] = useState({ start: "", end: "" }),
    [rangeDraft, setRangeDraft] = useState({ start: "", end: "" });
  const [replay, setReplay] = useState<{ code: string; date: string } | null>(
      null,
    ),
    [replayDate, setReplayDate] = useState(""),
    [playing, setPlaying] = useState(false),
    [speed, setSpeed] = useState(1);
  const [anchor, setAnchor] = useState<PopoverAnchor | null>(null),
    [params, setParams] = useState<number[]>([]),
    [formError, setFormError] = useState("");
  const [tool, setTool] = useState("cursor"),
    [annotation, setAnnotation] = useState(""),
    [selected, setSelected] = useState(false);
  const [side, setSide] = useState(() => innerWidth > 1080),
    [fullscreen, setFullscreen] = useState(false),
    [notice, setNotice] = useState("");
  const [inspected, setInspected] = useState<Bar | null>(null),
    [visibleRange, setVisibleRange] = useState(""),
    [exactTable, setExactTable] = useState(false);
  const root = useRef<HTMLElement>(null),
    chart = useRef<ChartHandle>(null),
    sideTrigger = useRef<HTMLButtonElement>(null);
  const authError = useCallback(
    (caught: unknown) => {
      if ((caught instanceof DataCollectionApiError || caught instanceof MarketApiError) && caught.status === 401) {
        logout();
        navigate("/login", { replace: true });
      }
    },
    [logout, navigate],
  );
  useEffect(() => {
    setSeriesKey("etf-daily-local");
    setAdjustment("raw");
    setReplay(null);
    setPlaying(false);
    setInspected(null);
    setRange({ start: "", end: "" });
    setExactTable(false);
    setAnchor(null);
    setTool("cursor");
    setNotice("");
  }, [tsCode, entry?.instrumentKey]);
  useEffect(() => {
    const sync = () =>
      setFullscreen(document.fullscreenElement === root.current);
    document.addEventListener("fullscreenchange", sync);
    return () => document.removeEventListener("fullscreenchange", sync);
  }, []);
  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(""), 6000);
    return () => clearTimeout(timer);
  }, [notice]);
  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || anchor) return;
      chart.current?.cancel();
      setTool("cursor");
      if (innerWidth <= 1080 && side) {
        setSide(false);
        sideTrigger.current?.focus();
      }
    };
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  }, [anchor, side]);
  const cutoff = replay?.code === tsCode ? replay.date : undefined;
  const marketAdjustment: MarketAdjustment = adjustment === "raw" ? "none" : adjustment === "forward" ? "pre" : "post";
  const requestedEnd = cutoff ?? (range.end || fixedEnd);
  const selectedIdentity = entry?.instrumentKey ?? tsCode;
  const formal = useFormalDetail(selectedIdentity, { seriesKey, period, adjustment: marketAdjustment, start: range.start || undefined,
    end: requestedEnd, asOf: requestedEnd, refresh }, authError);
  const identity = formal.identity.data;
  const currentIdentity = identity?.requestedSymbol === selectedIdentity;
  const candidate = formal.prices.data;
  const data = candidate && currentIdentity && matchesBarWindow(candidate, { instrument_key: identity!.instrument_key, series_key: seriesKey,
    period, adjustment: marketAdjustment, start: range.start || undefined, end: requestedEnd, as_of: requestedEnd }) ? candidate : null;
  const etf = currentIdentity ? formal.metadata.data?.instrument : undefined;
  const entrySymbol = entry?.symbol?.startsWith("instrument:") ? null : entry?.symbol;
  const displayCode = etf?.symbol ?? entrySymbol ?? (tsCode.startsWith("instrument:") ? "ETF" : tsCode);
  const loading = formal.prices.pending || formal.identity.pending;
  const selectedSeries = currentIdentity ? formal.options.data?.find(option => option.series_key === seriesKey) ?? null : null;
  const error = formal.prices.error?.message ?? formal.identity.error?.message ?? "";
  const series = useMemo(() => data ? plotBars(data) : { bars: [], error: "", volumeMissing: false, gaps: false }, [data]);
  const daily = (replaySteps.code === tsCode ? replaySteps.dates : []).map(trade_date => ({ trade_date }));
  useEffect(() => {
    if (data && data.period === "day" && !cutoff && !formal.prices.stale)
      setReplaySteps({ code: tsCode, dates: data.items.map(row => row.last_trade_date) });
  }, [data, cutoff, formal.prices.stale, tsCode]);
  useEffect(() => {
    if (formal.prices.error) { setPlaying(false); setInspected(null); }
    if (!data) setInspected(null);
  }, [formal.prices.error, formal.prices.stale, data]);
  const replayIndex = cutoff ? daily.findIndex(row => row.trade_date === cutoff) : -1;
  useEffect(() => {
    if (!playing || !cutoff || replayIndex < 0 || loading) return;
    if (replayIndex >= daily.length - 1 || formal.prices.stale || !data) { setPlaying(false); return; }
    const timer = setTimeout(() => setReplay({ code: tsCode, date: daily[replayIndex + 1].trade_date }), 1000 / speed);
    return () => clearTimeout(timer);
  }, [playing, cutoff, replayIndex, daily, speed, tsCode, loading, formal.prices.stale, data]);
  const latest = series.bars.at(-1), bar = inspected && series.bars.some(item => item.timestamp === inspected.timestamp) ? inspected : latest;
  const inRange = Boolean(series.bars.length);
  const open = (key: string, element: HTMLElement) => {
    setFormError("");
    setAnchor((prior) => (prior?.key === key ? null : { key, element }));
  };
  const close = () => setAnchor(null);
  const chooseTool = (name: string, element: HTMLElement) => {
    if (!series.bars.length) return;
    setPlaying(false);
    if (name === "simpleAnnotation") {
      setAnnotation("");
      open("annotation", element);
      return;
    }
    chart.current?.cancel();
    if (name !== "cursor" && name !== "zoom") chart.current?.draw(name);
    setTool(name);
  };
  const toggleFullscreen = async () => {
    try {
      if (document.fullscreenElement === root.current)
        await document.exitFullscreen();
      else await root.current?.requestFullscreen();
    } catch {
      setNotice("无法切换全屏，请检查浏览器权限后重试。");
    }
  };
  const selectCode = (code: string) => {
    if (code !== tsCode)
      navigate(`/admin/data/etf-basics/${encodeURIComponent(code)}`, {
        state: {
          marketReturnTo: marketReturnTo(location.state?.marketReturnTo),
        },
      });
  };
  const setInfoTab = (value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value === "basic") next.delete("tab");
    else next.set("tab", value);
    setSearchParams(next, { state: location.state, replace: true });
  };
  const indicatorKey = anchor?.key.startsWith("params:")
    ? anchor.key.slice(7)
    : "";
  return (
    <section ref={root} className="qfe" aria-label="ETF 行情工作区">
      <header className="qfe-topbar">
        <div className="qfe-brand">
          <span className="qfe-mark">QF</span>
          <strong>Quant Foundry</strong>
        </div>
        <div className="qfe-crumb">
          <button
            className="qfe-back"
            title="返回 A 股市场"
            onClick={() =>
              navigate(marketReturnTo(location.state?.marketReturnTo))
            }
          >
            <ArrowLeft />
            <span>返回 A 股市场</span>
          </button>
          <span className="qfe-breadcrumb">
            MARKET / A-SHARE / ETF WORKSPACE
          </span>
        </div>
        <div className="qfe-quotes" title="未接入实时买卖盘">
          <span>
            卖 <strong>—</strong>
          </span>
          <span>
            买 <strong>—</strong>
          </span>
        </div>
        <div className="qfe-symbol">{displayCode}</div>
        <div className="qfe-top-actions">
          <button
            ref={sideTrigger}
            title="侧栏"
            aria-label="切换资料侧栏"
            aria-expanded={side}
            onClick={() => setSide((v) => !v)}
          >
            <PanelRight />
            <span>侧栏</span>
          </button>
          <button
            className="qfe-top-alert"
            title="警报未接入"
            aria-label="警报未接入"
            aria-disabled="true"
            onClick={() =>
              setNotice("价格警报尚未接入；本页用于历史行情研究。")
            }
          >
            <Bell />
          </button>
          <button
            className="qfe-primary"
            title="刷新数据"
            aria-label="刷新数据"
            disabled={loading}
            onClick={() => {
              setPlaying(false);
              if (!cutoff && !range.end) setFixedEnd(shanghaiBusinessDate());
              setRefresh((v) => v + 1);
            }}
          >
            <RefreshCw className={loading ? "spin" : ""} />
            <span>{loading ? "刷新中…" : "刷新数据"}</span>
          </button>
        </div>
      </header>
      <div className={`qfe-workspace ${side ? "qfe-side-open" : ""}`}>
        <aside className="qfe-tools" aria-label="绘图工具">
          {[
            ["cursor", MousePointer2],
            ["segment", Slash],
            ["parallelStraightLine", ChevronsUpDown],
            ["fibonacciLine", ChartCandlestick],
            ["simpleAnnotation", TextCursorInput],
            ["qfRuler", Ruler],
            ["zoom", ZoomIn],
          ].map(([name, Icon]) => {
            const key = name as string,
              ToolIcon = Icon as typeof MousePointer2;
            return (
              <button
                key={key}
                className={tool === key ? "active" : ""}
                aria-label={TOOL_LABELS[key]}
                title={TOOL_LABELS[key]}
                aria-pressed={tool === key}
                disabled={!series.bars.length}
                onClick={(e) => chooseTool(key, e.currentTarget)}
              >
                <ToolIcon />
              </button>
            );
          })}
          <button
            aria-label="删除选中绘图"
            title="删除选中绘图"
            disabled={!selected}
            onClick={() => chart.current?.removeSelected()}
          >
            <Trash2 />
          </button>
        </aside>
        <div className="qfe-chart-top">
          <span className="qfe-control-label">周期</span>
          <button onClick={(e) => open("period", e.currentTarget)}>
            1 {PERIODS[period]}
            <ChevronDown />
          </button>
          <div className="qfe-button-group">
            <button onClick={(e) => open("indicators", e.currentTarget)}>
              指标
            </button>
            <button
              aria-disabled="true"
              onClick={() =>
                setNotice("价格警报尚未接入；本页用于历史行情研究。")
              }
            >
              警报
            </button>
            <button
              className={cutoff ? "active" : ""}
              disabled={!daily.length}
              onClick={(e) => {
                setPlaying(false);
                setReplayDate(
                  cutoff ??
                    daily[Math.max(0, daily.length - 30)]?.trade_date ??
                    "",
                );
                open("replay", e.currentTarget);
              }}
            >
              回放
            </button>
          </div>
          <div className="qfe-ma-buttons">
            <span className="qfe-control-label">均线</span>
            {indicators.ma.map((n, i) => (
              <button
                key={i}
                aria-pressed={indicators.MA && indicators.maVisible[i]}
                className={
                  indicators.MA && indicators.maVisible[i] ? "" : "off"
                }
                onClick={() =>
                  setIndicators((v) => ({
                    ...v,
                    MA: true,
                    maVisible: v.maVisible.map((enabled, j) =>
                      j === i ? !enabled : enabled,
                    ),
                  }))
                }
              >
                <i style={{ background: MA_COLORS[i] }} />
                MA{n}
              </button>
            ))}
          </div>
          <span className="qfe-spacer" />
          <button
            onClick={(e) => {
              setRangeDraft(range);
              open("range", e.currentTarget);
            }}
          >
            范围
          </button>
          <button
            aria-label={fullscreen ? "退出全屏" : "全屏"}
            title={fullscreen ? "退出全屏" : "全屏"}
            className="qfe-icon"
            onClick={() => void toggleFullscreen()}
          >
            <Expand />
          </button>
          {currentIdentity && formal.options.data && (formal.options.data.length > 1 || !selectedSeries) && <label className="qfe-series">价格序列
            <select value={seriesKey} onChange={event => {
              setSeriesKey(event.target.value); setAdjustment("raw"); setPlaying(false); setInspected(null); setExactTable(false);
            }}>
              {!selectedSeries && <option value={seriesKey} disabled>所选序列当前不可用</option>}
              {formal.options.data.map(option => <option key={option.series_key} value={option.series_key}>{option.label}</option>)}
            </select>
          </label>}
          {BasisControls && formal.identity.data ? <BasisControls instrumentKey={formal.identity.data.instrument_key}
            series={selectedSeries} adjustment={marketAdjustment} asOf={cutoff ?? (range.end || fixedEnd)}
            disabled={loading} onChange={value => { setAdjustment(value === "none" ? "raw" : value === "pre" ? "forward" : "backward"); setPlaying(false); setInspected(null); }} /> :
            <button onClick={(e) => open("adjust", e.currentTarget)}>{ADJUSTMENTS[adjustment]}<ChevronDown /></button>}
        </div>
        <main className="qfe-stage" aria-busy={loading}>
          <div className="qfe-chart-caption">
            <strong>{displayCode}</strong>
            <span>
              {ADJUSTMENTS[adjustment]} · {PERIODS[period]}线
              {cutoff ? ` · 回放至 ${dateText(cutoff)}` : ""}
            </span>
          </div>
          <div className="qfe-ohlc" aria-label="当前查看的行情" aria-live="off">
            <span>{dateText(bar?.date)}</span>
            {(
              [
                ["开", bar?.open],
                ["高", bar?.high],
                ["低", bar?.low],
                ["收", bar?.close],
              ] as const
            ).map(([label]) => (
              <span key={label}>
                {label} <strong>{exactMarketValue(bar?.exact?.[label === "开" ? "open" : label === "高" ? "high" : label === "低" ? "low" : "close"] ?? null)}</strong>
              </span>
            ))}
            <span>
              量 <strong>{exactBarValue(bar, "volume")}</strong> {data?.units.volume ?? ""}
            </span>
            <span>
              额 <strong>{exactBarValue(bar, "amount")}</strong> {data?.units.amount ?? ""}
            </span>
          </div>
          <div className="qfe-plot">
            <EtfChart
              ref={chart}
              bars={series.bars}
              code={displayCode}
              basis={adjustment}
              period={period}
              indicators={series.gaps ? { ...indicators, MA: false, MACD: false, RSI: false } : indicators}
              volumeMissing={series.volumeMissing || series.gaps}
              replay={Boolean(cutoff)}
              range={range}
              onInspect={setInspected}
              onRange={setVisibleRange}
              onEarlier={formal.earlier}
              canLoadEarlier={Boolean(data?.next_cursor && formal.prices.canContinue && !loading)}
              onDrawEnd={() => setTool("cursor")}
              onSelection={setSelected}
            />
            <div className="qfe-watermark" aria-hidden="true">
              Quant Foundry
            </div>
            {(!data || !series.bars.length || !inRange) && (
              <div className="qfe-chart-empty" role="status">
                {!data
                  ? loading
                    ? "正在加载 ETF 行情…"
                    : error || "ETF 数据不可用。"
                  : series.error ||
                    (!series.bars.length
                      ? "该窗口内没有可读取的正式日线。"
                      : "所选范围内没有行情，请调整日期范围。")}
              </div>
            )}
            {tool !== "cursor" && (
              <div className="qfe-tool-status">
                {tool === "zoom" ? (
                  <>
                    <span>缩放视口</span>
                    <button
                      aria-label="缩小图表"
                      onClick={() => chart.current?.zoom(0.8)}
                    >
                      <Minus />
                    </button>
                    <button
                      aria-label="放大图表"
                      onClick={() => chart.current?.zoom(1.25)}
                    >
                      <ZoomIn />
                    </button>
                    <button onClick={() => chart.current?.reset()}>
                      <Maximize />
                      重置
                    </button>
                  </>
                ) : (
                  <span>{TOOL_HINTS[tool]} · Esc 取消</span>
                )}
                <button
                  aria-label="退出绘图工具"
                  onClick={() => {
                    chart.current?.cancel();
                    setTool("cursor");
                  }}
                >
                  <X />
                </button>
              </div>
            )}
            {cutoff && (
              <div className="qfe-replay" aria-label="历史回放控制">
                <span className="qfe-replay-label">回放</span>
                <strong>{dateText(cutoff)}</strong>
                <button
                  aria-label={playing ? "暂停回放" : "播放回放"}
                  disabled={
                    !data || loading || formal.prices.stale ||
                    replayIndex < 0 ||
                    replayIndex >= daily.length - 1 ||
                    Boolean(series.error)
                  }
                  onClick={() => setPlaying((v) => !v)}
                >
                  {playing ? <Pause /> : <Play />}
                </button>
                <button
                  aria-label="前进一根日线"
                  disabled={
                    !data || loading || formal.prices.stale ||
                    replayIndex < 0 ||
                    replayIndex >= daily.length - 1 ||
                    Boolean(series.error)
                  }
                  onClick={() => {
                    setPlaying(false);
                    setReplay({
                      code: tsCode,
                      date: daily[replayIndex + 1].trade_date,
                    });
                  }}
                >
                  <SkipForward />
                </button>
                <select
                  aria-label="回放速度"
                  value={speed}
                  onChange={(e) => setSpeed(Number(e.target.value))}
                >
                  {[1, 2, 4].map((v) => (
                    <option key={v} value={v}>
                      {v}×
                    </option>
                  ))}
                </select>
                <button
                  onClick={() => {
                    setPlaying(false);
                    setReplay(null);
                    setInspected(null);
                  }}
                >
                  退出
                </button>
                {replayIndex === daily.length - 1 && (
                  <small>已到最后一条</small>
                )}
              </div>
            )}
          </div>
          <div className="qfe-data-note" role={error ? "alert" : "status"}>
            {error ? `${error}${formal.prices.stale ? " 显示同条件旧数据，已停止分页与回放。" : ""}` : loading ? "正在读取正式数据…" : series.error ||
              (series.gaps ? "正式日历发现缺日；未补值，连续指标与量能副图暂停。" : data?.meta.coverage.note ?? "价格与资料分别加载。")}
            {series.volumeMissing && " 部分量能无法显示；精确量额可在原值表查看。"}
            {data?.items.some(item => item.completion === "partial") && " 部分周期未完成或缺少必要日线。"}
          </div>
          <div className="qfe-history-actions">
            <button disabled={!data?.next_cursor || !formal.prices.canContinue || loading || data.items.length > 9488}
              onClick={formal.earlier}>加载更早历史</button>
            <button disabled={!data?.items.length} onClick={() => setExactTable(value => !value)}>精确值表</button>
            <span>{data?.items.length ?? 0} / 10,000 根 · 指标左端须满参数窗口</span>
            {data && data.items.length > 9488 && <button onClick={() => { formal.release(); setRange({ start: "", end: "" }); setRefresh(value => value + 1); }}>释放窗口并重读</button>}
          </div>
          {exactTable && data && <div className="qfe-exact-values" tabIndex={0} aria-label="精确价格与量额">
            <table><thead><tr><th>业务日</th><th>开</th><th>高</th><th>低</th><th>收</th><th>量 ({data.units.volume})</th><th>额 ({data.units.amount})</th><th>周期状态</th></tr></thead>
              <tbody>{data.items.slice(-100).map(item => <tr key={item.bucket_start}><td>{item.last_trade_date}</td>
                {[item.open, item.high, item.low, item.close, item.volume, item.amount].map((value, index) => <td key={index}>{exactMarketValue(value)}</td>)}<td>{({ complete: "完整", partial: "部分", unknown: "未知" })[item.completion]}</td></tr>)}</tbody></table>
            <small>当前窗口最近 100 根原始精确值，可选择复制。</small></div>}
        </main>
        {side && (
          <>
            <button
              className="qfe-side-scrim"
              aria-label="关闭资料侧栏"
              onClick={() => {
                setSide(false);
                sideTrigger.current?.focus();
              }}
            />
            <aside className="qfe-right" aria-label="ETF 资料与自选">
              <EtfWatchlist
                code={displayCode}
                replay={Boolean(cutoff)}
                refresh={refresh}
                onSelect={selectCode}
                onClose={() => {
                  setSide(false);
                  sideTrigger.current?.focus();
                }}
                onError={authError}
              />
              <div className="qfe-info">
                <div className="qfe-summary">
                  <div className="qfe-summary-top">
                    <span className="qfe-token">ETF</span>
                    <div>
                      <h1>{displayCode}</h1>
                      <p>
                        {exchangeName(etf?.exchange)} ·{" "}
                        {etf?.name ?? "ETF"}
                      </p>
                    </div>
                  </div>
                  <div className="qfe-summary-price">
                    <strong>{exactBarValue(latest, "close")}</strong>
                    <span>涨跌字段未知</span>
                  </div>
                  <p className="qfe-price-basis">
                    {dateText(latest?.date)} · {ADJUSTMENTS[adjustment]}{PERIODS[period]}收盘
                    {cutoff ? " · 回放" : ""}
                  </p>
                  {formal.metadata.error && <p className="qfe-error">资料：{formal.metadata.error.message}{formal.metadata.stale ? " 当前显示同条件旧资料。" : ""}</p>}
                  <dl className="qfe-summary-grid">
                    <Kv label="跟踪指数">{etf?.index_name ?? "—"}</Kv>
                    <Kv label="基金管理人">{etf?.manager_name ?? "—"}</Kv>
                    <Kv label="上市日期">{dateText(etf?.list_date)}</Kv>
                    <Kv label="管理费率">
                      {etf?.management_fee == null
                        ? "—"
                        : `${etf?.management_fee} ${etf?.management_fee_unit ?? ""}`}
                    </Kv>
                  </dl>
                </div>
                <div
                  className="qfe-info-tabs"
                  role="tablist"
                  aria-label="ETF 资料页签"
                >
                  {(
                    [
                      ["basic", "基础资料"],
                      ["overview", "概览"],
                      ["factors", "复权因子"],
                    ] as const
                  ).map(([key, label]) => (
                    <button
                      key={key}
                      role="tab"
                      id={`qfe-tab-${key}`}
                      aria-selected={tab === key}
                      aria-controls="qfe-info-panel"
                      onClick={() => setInfoTab(key)}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <div
                  className="qfe-info-panel"
                  id="qfe-info-panel"
                  role="tabpanel"
                  aria-labelledby={`qfe-tab-${tab}`}
                >
                  {tab === "basic" && (
                    <dl className="qfe-kv">
                      <Kv label="基金代码">{displayCode}</Kv>
                      <Kv label="基金名称">
                        {etf?.name ?? "—"}
                      </Kv>
                      <Kv label="交易所">{exchangeName(etf?.exchange)}</Kv>
                      <Kv label="指数代码">{etf?.index_name ?? "—"}</Kv>
                      <Kv label="上市状态">
                        {({ L: "上市", D: "退市", P: "发行" }[
                          etf?.list_status ?? ""
                        ] ??
                          etf?.list_status) ||
                          "—"}
                      </Kv>
                      <Kv label="ETF 类型">{etf?.asset_type ?? "—"}</Kv>
                    </dl>
                  )}
                  {tab === "overview" && <>
                    <p className="qfe-note">默认请求最近 512 根输出，向左回看按需加载。日期范围重新读取有界历史；均线只计算已加载且满参数的窗口。回放查询当前修订数据，未到截止日的行情不会出现。</p>
                    <dl className="qfe-kv"><Kv label="已加载输出">{data?.items.length ?? 0} 根</Kv>
                      <Kv label="业务截至">{dateText(data?.meta.business_as_of)}</Kv><Kv label="读取时点">{timeText(data?.meta.read_at)}</Kv>
                      <Kv label="来源">{data?.meta.source_labels.join(" · ") ?? "—"}</Kv>
                      <Kv label="日期覆盖">{data?.meta.coverage.note ?? "未知"}</Kv></dl>
                  </>}
                  {tab === "factors" && (FactorTable && formal.identity.data ? <FactorTable
                    instrumentKey={formal.identity.data.instrument_key} seriesKey={seriesKey} start={range.start || null}
                    end={cutoff ?? (range.end || fixedEnd)} asOf={cutoff ?? (range.end || fixedEnd)} adjustment={marketAdjustment}
                    page={data} onError={formal.factorError} /> : <p className="qfe-note">因子表由独立口径模块接入。不复权价格无需因子；前/后复权暂不可选择。</p>)}
                </div>
              </div>
            </aside>
          </>
        )}
        <footer className="qfe-timeline">
          <span>
            {series.bars.length && inRange ? visibleRange : "暂无可见行情"}
          </span>
          <span>历史行情 · UTC+8</span>
        </footer>
      </div>
      {notice && (
        <div className="qfe-toast" role="status">
          {notice}
        </div>
      )}
      {anchor && (
        <EtfPopover
          anchor={anchor}
          title={
            indicatorKey
              ? `${indicatorKey} 参数`
              : ({
                  period: "周期",
                  adjust: "复权",
                  indicators: "指标",
                  range: "日期范围",
                  replay: "历史回放",
                  annotation: "标注",
                }[anchor.key] ?? "设置")
          }
          close={close}
        >
          {anchor.key === "period" &&
            (Object.keys(PERIODS) as Period[]).map((key) => (
              <button
                className="qfe-menu-item"
                aria-pressed={period === key}
                key={key}
                onClick={() => {
                  setPeriod(key);
                  setInspected(null);
                  close();
                }}
              >
                1 {PERIODS[key]} {period === key ? "✓" : ""}
              </button>
            ))}
          {anchor.key === "adjust" &&
            (Object.keys(ADJUSTMENTS) as Adjustment[]).map((key) => (
              <button
                className="qfe-menu-item"
                aria-pressed={adjustment === key}
                key={key}
                disabled={key !== "raw"}
                title={key === "raw" ? "正式不复权成交价" : "独立复权模块尚未接入"}
                onClick={() => {
                  setAdjustment(key);
                  setInspected(null);
                  close();
                }}
              >
                {ADJUSTMENTS[key]} {adjustment === key ? "✓" : ""}
              </button>
            ))}
          {anchor.key === "indicators" && (
            <>
              <p>主图均线与副图分别管理；关闭不会清除参数。</p>
              {(["MA", "VOL", "MACD", "RSI"] as const).map((key) => (
                <div className="qfe-field" key={key}>
                  <label>
                    <input
                      type="checkbox"
                      checked={indicators[key]}
                      onChange={(e) =>
                        setIndicators((v) => ({
                          ...v,
                          [key]: e.target.checked,
                        }))
                      }
                    />
                    {
                      {
                        MA: "均线 MA",
                        VOL: "成交量 VOL",
                        MACD: "MACD",
                        RSI: "RSI",
                      }[key]
                    }
                  </label>
                  <button
                    onClick={() => {
                      setParams(
                        key === "MA"
                          ? [...indicators.ma]
                          : key === "VOL"
                            ? [indicators.vol]
                            : key === "MACD"
                              ? [...indicators.macd]
                              : [indicators.rsi],
                      );
                      setAnchor({ ...anchor, key: `params:${key}` });
                    }}
                  >
                    {"参数"}
                  </button>
                </div>
              ))}
            </>
          )}
          {indicatorKey && (
            <>
              <p>
                周期为 1–250 的整数
                {indicatorKey === "MACD" ? "，慢线周期需大于快线" : ""}。
              </p>
              {params.map((value, i) => (
                <div className="qfe-field" key={i}>
                  <label htmlFor={`qfe-param-${i}`}>
                    {indicatorKey === "MA"
                      ? `均线 ${i + 1}`
                      : indicatorKey === "MACD"
                        ? ["快线", "慢线", "信号"][i]
                        : "周期"}
                  </label>
                  <input
                    id={`qfe-param-${i}`}
                    type="number"
                    min="1"
                    max="250"
                    step="1"
                    value={Number.isNaN(value) ? "" : value}
                    onChange={(e) =>
                      setParams((v) =>
                        v.map((n, j) => (i === j ? e.target.valueAsNumber : n)),
                      )
                    }
                  />
                </div>
              ))}
              <div className="qfe-actions">
                <button
                  onClick={() => {
                    setFormError("");
                    setAnchor({ ...anchor, key: "indicators" });
                  }}
                >
                  取消
                </button>
                <button
                  className="qfe-primary"
                  onClick={() => {
                    if (!validParams(indicatorKey, params)) {
                      setFormError("请填写有效周期；MACD 慢线必须大于快线。");
                      return;
                    }
                    setIndicators((v) => ({
                      ...v,
                      ...(indicatorKey === "MA"
                        ? { ma: [...params] }
                        : indicatorKey === "MACD"
                          ? { macd: [...params] }
                          : indicatorKey === "RSI"
                            ? { rsi: params[0] }
                            : { vol: params[0] }),
                    }));
                    setFormError("");
                    setAnchor({ ...anchor, key: "indicators" });
                  }}
                >
                  应用
                </button>
              </div>
            </>
          )}
          {anchor.key === "range" && (
            <>
              <label className="qfe-field-stack">
                开始日期
                <input
                  type="date"
                  value={rangeDraft.start}
                  onChange={(e) =>
                    setRangeDraft((v) => ({ ...v, start: e.target.value }))
                  }
                />
              </label>
              <label className="qfe-field-stack">
                结束日期
                <input
                  type="date"
                  value={rangeDraft.end}
                  onChange={(e) =>
                    setRangeDraft((v) => ({ ...v, end: e.target.value }))
                  }
                />
              </label>
              <p>留空读取最近 512 根；明确日期范围仍分页读取。视口以所选范围定位，左端不足参数窗口时指标不可计算。</p>
              <div className="qfe-actions">
                <button
                  onClick={() => {
                    setRange({ start: "", end: "" });
                    setExactTable(false);
                    chart.current?.reset();
                    close();
                  }}
                >
                  重置
                </button>
                <button
                  className="qfe-primary"
                  onClick={() => {
                    if (
                      rangeDraft.start &&
                      rangeDraft.end &&
                      rangeDraft.start > rangeDraft.end
                    ) {
                      setFormError("开始日期不能晚于结束日期。");
                      return;
                    }
                    setRange(rangeDraft);
                    setExactTable(false);
                    close();
                  }}
                >
                  应用
                </button>
              </div>
            </>
          )}
          {anchor.key === "replay" && (
            <>
              <label className="qfe-field-stack">
                开始回放日期
                <input
                  type="date"
                  min={daily[0]?.trade_date}
                  max={daily.at(-1)?.trade_date}
                  value={replayDate}
                  onChange={(e) => setReplayDate(e.target.value)}
                />
              </label>
              <p>
                从已加载日线的首个日期开始；每步按截止日重新读取当前修订数据，周/月/年线同步更新。切换周期前可先用日线加载所需回放日期。
              </p>
              <div className="qfe-actions">
                <button onClick={close}>取消</button>
                <button
                  className="qfe-primary"
                  onClick={() => {
                    if (range.start && replayDate < range.start) {
                      setFormError("回放截止不能早于查询开始日期。");
                      return;
                    }
                    const first = daily.find(
                      (row) => row.trade_date >= replayDate,
                    );
                    if (
                      !replayDate ||
                      !first ||
                      replayDate < daily[0].trade_date
                    ) {
                      setFormError("请选择已加载日线范围内的日期。");
                      return;
                    }
                    setPlaying(false);
                    setReplay({ code: tsCode, date: first.trade_date });
                    setExactTable(false);
                    setInspected(null);
                    close();
                  }}
                >
                  开始回放
                </button>
              </div>
            </>
          )}
          {anchor.key === "annotation" && (
            <>
              <label className="qfe-field-stack">
                标注文字
                <input
                  maxLength={80}
                  value={annotation}
                  onChange={(e) => setAnnotation(e.target.value)}
                />
              </label>
              <div className="qfe-actions">
                <button onClick={close}>取消</button>
                <button
                  className="qfe-primary"
                  onClick={() => {
                    if (!annotation.trim()) {
                      setFormError("请输入标注文字。");
                      return;
                    }
                    chart.current?.draw("simpleAnnotation", annotation.trim());
                    setTool("simpleAnnotation");
                    close();
                  }}
                >
                  放置标注
                </button>
              </div>
            </>
          )}
          {formError && (
            <p className="qfe-error" role="alert">
              {formError}
            </p>
          )}
        </EtfPopover>
      )}
    </section>
  );
}
