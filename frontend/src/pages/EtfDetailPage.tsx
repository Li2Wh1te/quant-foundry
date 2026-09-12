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
import {
  DataCollectionApiError,
  getEtf,
  listEtfAdjustmentFactors,
  listEtfDailyBars,
  type EtfAdjustmentFactor,
  type EtfCode,
  type EtfDailyBar,
} from "../api/dataCollections";
import { useAuth } from "../auth/AuthContext";
import { marketReturnTo } from "./market/marketPresentation";
import {
  ADJUSTMENTS,
  buildSeries,
  dateText,
  DEFAULT_INDICATORS,
  direction,
  exchangeName,
  MA_COLORS,
  numberText,
  numeric,
  PERIODS,
  signed,
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

interface Snapshot {
  code: string;
  etf: EtfCode;
  daily: EtfDailyBar[];
  factors: EtfAdjustmentFactor[];
  factorError: string;
}
const EMPTY_DAILY: EtfDailyBar[] = [],
  EMPTY_FACTORS: EtfAdjustmentFactor[] = [];
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

export function EtfDetailPage() {
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
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null),
    [loading, setLoading] = useState(true),
    [error, setError] = useState(""),
    [refresh, setRefresh] = useState(0);
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
    [factorPage, setFactorPage] = useState(0);
  const root = useRef<HTMLElement>(null),
    chart = useRef<ChartHandle>(null),
    sideTrigger = useRef<HTMLButtonElement>(null);
  const authError = useCallback(
    (caught: unknown) => {
      if (caught instanceof DataCollectionApiError && caught.status === 401) {
        logout();
        navigate("/login", { replace: true });
      }
    },
    [logout, navigate],
  );
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    void (async () => {
      const results = await Promise.allSettled([
        getEtf(tsCode, controller.signal),
        listEtfDailyBars(tsCode, {}, controller.signal),
        listEtfAdjustmentFactors(tsCode, {}, controller.signal),
      ]);
      if (controller.signal.aborted) return;
      for (const result of results)
        if (result.status === "rejected") authError(result.reason);
      if (results[0].status === "rejected") throw results[0].reason;
      if (results[1].status === "rejected") throw results[1].reason;
      const etf = results[0].value,
        daily = [...results[1].value].sort((a, b) =>
          a.trade_date.localeCompare(b.trade_date),
        );
      const factorResult = results[2];
      setSnapshot((prior) => ({
        code: tsCode,
        etf,
        daily,
        factors:
          factorResult.status === "fulfilled"
            ? factorResult.value
            : prior?.code === tsCode
              ? prior.factors
              : [],
        factorError:
          factorResult.status === "fulfilled"
            ? ""
            : `复权因子刷新失败：${message(factorResult.reason)}`,
      }));
    })()
      .catch((caught) => {
        if (!controller.signal.aborted) setError(message(caught));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [tsCode, refresh, authError]);
  useEffect(() => {
    setReplay(null);
    setPlaying(false);
    setInspected(null);
    setRange({ start: "", end: "" });
    setFactorPage(0);
    setAnchor(null);
    setTool("cursor");
    setNotice("");
  }, [tsCode]);
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
  const data = snapshot?.code === tsCode ? snapshot : null,
    etf = data?.etf;
  const daily = data?.daily ?? EMPTY_DAILY,
    factors = data?.factors ?? EMPTY_FACTORS;
  const cutoff = replay?.code === tsCode ? replay.date : undefined;
  const replayIndex = cutoff
    ? daily.findIndex((row) => row.trade_date === cutoff)
    : -1;
  useEffect(() => {
    if (!playing || !cutoff || replayIndex < 0) return;
    if (replayIndex >= daily.length - 1) {
      setPlaying(false);
      return;
    }
    const timer = setTimeout(
      () =>
        setReplay({ code: tsCode, date: daily[replayIndex + 1].trade_date }),
      1000 / speed,
    );
    return () => clearTimeout(timer);
  }, [playing, cutoff, daily, replayIndex, speed, tsCode]);
  const series = useMemo(
    () => buildSeries(daily, factors, adjustment, period, cutoff),
    [daily, factors, adjustment, period, cutoff],
  );
  const dailyVisible = useMemo(
    () => daily.filter((row) => !cutoff || row.trade_date <= cutoff),
    [daily, cutoff],
  );
  const latest = dailyVisible.at(-1),
    bar =
      inspected &&
      series.bars.some((item) => item.timestamp === inspected.timestamp)
        ? inspected
        : series.bars.at(-1);
  const visibleFactors = useMemo(
    () =>
      factors
        .filter(
          (row) =>
            (!cutoff || row.trade_date <= cutoff) &&
            (!range.start || row.trade_date >= range.start) &&
            (!range.end || row.trade_date <= range.end),
        )
        .sort((a, b) => b.trade_date.localeCompare(a.trade_date)),
    [factors, cutoff, range],
  );
  useEffect(() => {
    setFactorPage((page) =>
      Math.min(page, Math.max(0, Math.ceil(visibleFactors.length / 20) - 1)),
    );
  }, [visibleFactors.length]);
  const inRange =
    Boolean(cutoff) ||
    series.bars.some(
      (row) =>
        (!range.start || row.date >= range.start) &&
        (!range.end || row.date <= range.end),
    );
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
        <div className="qfe-symbol">{tsCode}</div>
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
          <button onClick={(e) => open("adjust", e.currentTarget)}>
            {ADJUSTMENTS[adjustment]}
            <ChevronDown />
          </button>
        </div>
        <main className="qfe-stage" aria-busy={loading}>
          <div className="qfe-chart-caption">
            <strong>{tsCode}</strong>
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
            ).map(([label, value]) => (
              <span key={label}>
                {label} <strong>{numberText(value)}</strong>
              </span>
            ))}
            <span>
              量 <strong>{numberText(bar?.volume, 2)}</strong> 手
            </span>
            <span>
              额 <strong>{numberText(bar?.turnover, 2)}</strong> 千元
            </span>
          </div>
          <div className="qfe-plot">
            <EtfChart
              ref={chart}
              bars={series.bars}
              code={tsCode}
              basis={adjustment}
              period={period}
              indicators={indicators}
              volumeMissing={series.volumeMissing}
              replay={Boolean(cutoff)}
              range={range}
              onInspect={setInspected}
              onRange={setVisibleRange}
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
                      ? "尚未采集日线行情。"
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
          {((error && data) ||
            data?.factorError ||
            series.volumeMissing ||
            (loading && data)) && (
            <div className="qfe-data-note" role={error ? "alert" : "status"}>
              {error && data
                ? `${error} 当前保留上次成功加载的数据。`
                : loading
                  ? "正在刷新，当前显示上次成功加载的数据。"
                  : data?.factorError ||
                    "部分成交量缺失，成交量副图暂不显示；价格图仍可查看。"}
            </div>
          )}
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
                code={tsCode}
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
                      <h1>{tsCode}</h1>
                      <p>
                        {exchangeName(etf?.exchange)} ·{" "}
                        {etf?.csname ?? etf?.extname ?? "ETF"}
                      </p>
                    </div>
                  </div>
                  <div className="qfe-summary-price">
                    <strong>{numberText(latest?.close)}</strong>
                    <span className={direction(latest?.change)}>
                      {signed(latest?.change)}
                    </span>
                    <span className={direction(latest?.pct_chg)}>
                      {numeric(latest?.pct_chg) === null
                        ? "—"
                        : `${signed(latest?.pct_chg, 2)}%`}
                    </span>
                  </div>
                  <p className="qfe-price-basis">
                    {dateText(latest?.trade_date)} · 不复权日收盘
                    {cutoff ? " · 回放" : ""}
                  </p>
                  {latest && numeric(latest.pct_chg) === null && (
                    <p className="qfe-price-basis">历史涨跌字段未采集</p>
                  )}
                  <dl className="qfe-summary-grid">
                    <Kv label="跟踪指数">{etf?.index_name ?? "—"}</Kv>
                    <Kv label="基金管理人">{etf?.mgr_name ?? "—"}</Kv>
                    <Kv label="上市日期">{dateText(etf?.list_date)}</Kv>
                    <Kv label="管理费率">
                      {numeric(etf?.mgt_fee) === null
                        ? "—"
                        : `${numberText(etf?.mgt_fee, 2)}%`}
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
                      <Kv label="基金代码">{tsCode}</Kv>
                      <Kv label="基金名称">
                        {etf?.cname ?? etf?.extname ?? etf?.csname ?? "—"}
                      </Kv>
                      <Kv label="交易所">{exchangeName(etf?.exchange)}</Kv>
                      <Kv label="指数代码">{etf?.index_code ?? "—"}</Kv>
                      <Kv label="上市状态">
                        {({ L: "上市", D: "退市", P: "发行" }[
                          etf?.list_status ?? ""
                        ] ??
                          etf?.list_status) ||
                          "—"}
                      </Kv>
                      <Kv label="ETF 类型">{etf?.etf_type ?? "—"}</Kv>
                    </dl>
                  )}
                  {tab === "overview" && (
                    <>
                      <p className="qfe-note">
                        加载全部已采集历史，默认显示最近 200 根 K
                        线。滚轮缩放，横向拖拽回看；日期范围只调整视口。盘口与价格警报尚未接入。
                      </p>
                      <dl className="qfe-kv">
                        <Kv label="日线记录">{dailyVisible.length} 条</Kv>
                        <Kv label="复权因子">
                          {
                            factors.filter(
                              (row) => !cutoff || row.trade_date <= cutoff,
                            ).length
                          }{" "}
                          条
                        </Kv>
                        <Kv label="日线覆盖">
                          {dateText(dailyVisible[0]?.trade_date)} —{" "}
                          {dateText(latest?.trade_date)}
                        </Kv>
                        <Kv label="数据来源">{latest?.source ?? "—"}</Kv>
                        <Kv label="最近同步">{timeText(latest?.updated_at)}</Kv>
                        <Kv label="量额单位">成交量：手 · 成交额：千元</Kv>
                      </dl>
                    </>
                  )}
                  {tab === "factors" && (
                    <>
                      {data?.factorError && (
                        <p className="qfe-error">{data.factorError}</p>
                      )}
                      <p className="qfe-price-basis">
                        按日期倒序 · {visibleFactors.length} 条
                        {cutoff ? " · 已隐藏未来日期" : ""}
                      </p>
                      <table className="qfe-factor-table">
                        <thead>
                          <tr>
                            <th>交易日期</th>
                            <th>复权因子</th>
                            <th>更新时间</th>
                          </tr>
                        </thead>
                        <tbody>
                          {visibleFactors
                            .slice(factorPage * 20, factorPage * 20 + 20)
                            .map((row) => (
                              <tr key={row.trade_date}>
                                <td>{dateText(row.trade_date)}</td>
                                <td title={`${row.adj_factor} · ${row.source}`}>
                                  {numberText(row.adj_factor, 6)}
                                </td>
                                <td>{timeText(row.updated_at)}</td>
                              </tr>
                            ))}
                        </tbody>
                      </table>
                      {!visibleFactors.length && (
                        <p className="qfe-note">该范围内没有复权因子。</p>
                      )}
                      {visibleFactors.length > 20 && (
                        <div className="qfe-actions">
                          <button
                            disabled={!factorPage}
                            onClick={() => setFactorPage((v) => v - 1)}
                          >
                            上一页
                          </button>
                          <span>
                            {factorPage + 1} /{" "}
                            {Math.ceil(visibleFactors.length / 20)}
                          </span>
                          <button
                            disabled={
                              (factorPage + 1) * 20 >= visibleFactors.length
                            }
                            onClick={() => setFactorPage((v) => v + 1)}
                          >
                            下一页
                          </button>
                        </div>
                      )}
                    </>
                  )}
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
              <p>留空表示全部历史。仅改变视口，指标保留历史预热。</p>
              <div className="qfe-actions">
                <button
                  onClick={() => {
                    setRange({ start: "", end: "" });
                    setFactorPage(0);
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
                    setFactorPage(0);
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
                从所选日期起的首个已采集交易日开始。每一步前进一条真实日线，周/月/年线随之更新。
              </p>
              <div className="qfe-actions">
                <button onClick={close}>取消</button>
                <button
                  className="qfe-primary"
                  onClick={() => {
                    const first = daily.find(
                      (row) => row.trade_date >= replayDate,
                    );
                    if (
                      !replayDate ||
                      !first ||
                      replayDate < daily[0].trade_date
                    ) {
                      setFormError("请选择已采集历史覆盖内的日期。");
                      return;
                    }
                    setPlaying(false);
                    setReplay({ code: tsCode, date: first.trade_date });
                    setFactorPage(0);
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
