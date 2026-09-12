import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import {
  init,
  dispose,
  registerOverlay,
  type Chart,
  type Options,
  type OverlayCreate,
  type Crosshair,
} from "klinecharts";
import { EtfDrawingInput, type DrawingInput } from "./EtfDrawingInput";
import {
  MA_COLORS,
  numberText,
  type Bar,
  type IndicatorSettings,
  type Period,
} from "./etfData";

registerOverlay({
  name: "qfRuler",
  totalStep: 3,
  needDefaultPointFigure: true,
  createPointFigures: ({ coordinates, overlay }) => {
    if (coordinates.length < 2) return [];
    const a = overlay.points[0],
      b = overlay.points[1];
    const difference = (b.value ?? 0) - (a.value ?? 0);
    const pct = a.value ? (difference / a.value) * 100 : null;
    const days =
      a.timestamp && b.timestamp
        ? Math.round(Math.abs(b.timestamp - a.timestamp) / 86400000)
        : 0;
    return [
      {
        type: "line",
        attrs: { coordinates },
        styles: { color: "#E85F32", size: 1 },
      },
      {
        type: "text",
        attrs: {
          x: coordinates[1].x,
          y: coordinates[1].y - 12,
          text: `${difference > 0 ? "+" : ""}${numberText(difference)} (${numberText(pct, 2)}%) · ${days} 自然日`,
          align: "center",
          baseline: "bottom",
        },
        styles: {
          color: "#e8edf3",
          backgroundColor: "#141922",
          size: 12,
          paddingLeft: 6,
          paddingRight: 6,
          paddingTop: 4,
          paddingBottom: 4,
        },
      },
    ];
  },
});
const options: Options = {
  locale: "zh-CN",
  timezone: "Asia/Shanghai",
  zoomAnchor: "cursor",
  hotkey: { enabled: false },
  decimalFold: { threshold: 100 },
  layout: {
    barSpaceLimit: { min: 1, max: 50 },
    yAxis: { position: "right", gap: { top: 0.2, bottom: 0.1 } },
  },
  styles: {
    grid: {
      horizontal: { color: "#202734", style: "dashed", dashedValue: [1, 5] },
      vertical: { color: "#202734", style: "dashed", dashedValue: [1, 5] },
    },
    candle: {
      bar: {
        compareRule: "current_open",
        upColor: "#D64E4E",
        downColor: "#2E9D68",
        noChangeColor: "#8a94a3",
        upBorderColor: "#D64E4E",
        downBorderColor: "#2E9D68",
        noChangeBorderColor: "#8a94a3",
        upWickColor: "#D64E4E",
        downWickColor: "#2E9D68",
        noChangeWickColor: "#8a94a3",
      },
      priceMark: {
        high: { show: false },
        low: { show: false },
        last: {
          show: true,
          upColor: "#D64E4E",
          downColor: "#2E9D68",
          noChangeColor: "#8a94a3",
        },
      },
      tooltip: { showRule: "none" },
    },
    indicator: {
      bars: [
        {
          upColor: "#D64E4E99",
          downColor: "#2E9D6899",
          noChangeColor: "#8a94a3",
        },
      ],
      lines: MA_COLORS.map((color) => ({ color, size: 1.5 })),
      lastValueMark: { show: false },
      tooltip: {
        showRule: "always",
        title: { size: 12, color: "#b8c0cc" },
        legend: { size: 12, color: "#b8c0cc", defaultValue: "—" },
      },
    },
    xAxis: {
      axisLine: { color: "#232833" },
      tickLine: { show: false },
      tickText: { color: "#aab4c4", size: 12 },
    },
    yAxis: {
      size: 76,
      axisLine: { color: "#232833" },
      tickLine: { show: false },
      tickText: { color: "#aab4c4", size: 12 },
    },
    separator: { color: "#232833", size: 1 },
    crosshair: {
      horizontal: {
        line: { color: "#798394" },
        text: { size: 12, backgroundColor: "#313846" },
      },
      vertical: {
        line: { color: "#798394" },
        text: { size: 12, backgroundColor: "#313846" },
      },
    },
    overlay: {
      line: { color: "#E85F32", size: 1 },
      text: { color: "#e8edf3", size: 12 },
    },
  },
};
interface View {
  space: number;
  timestamp?: number;
}
export interface ChartHandle {
  draw: (name: string, text?: string) => void;
  cancel: () => void;
  removeSelected: () => boolean;
  zoom: (scale: number) => void;
  reset: () => void;
}
interface Props {
  bars: Bar[];
  code: string;
  basis: string;
  period: Period;
  indicators: IndicatorSettings;
  volumeMissing: boolean;
  replay: boolean;
  range: { start: string; end: string };
  onInspect: (bar: Bar | null) => void;
  onRange: (value: string) => void;
  onDrawEnd: () => void;
  onSelection: (value: boolean) => void;
}
export const EtfChart = forwardRef<ChartHandle, Props>(
  function EtfChart(props, ref) {
    const element = useRef<HTMLDivElement>(null),
      chartRef = useRef<Chart | null>(null),
      current = useRef(props);
    current.current = props;
    const drawings = useRef(new Map<string, OverlayCreate[]>()),
      selected = useRef<string | null>(null);
    const [drawingInput, setDrawingInput] = useState<DrawingInput | null>(null);
    const previous = useRef({ code: "", basis: "", period: "", replay: false });
    const beforeReplay = useRef<View | null>(null);
    const lastValidView = useRef<View | null>(null);
    const view = (chart: Chart): View => ({
      space: chart.getBarSpace().bar,
      timestamp:
        chart.getDataList()[
          Math.min(
            chart.getDataList().length - 1,
            Math.max(0, Math.ceil(chart.getVisibleRange().to) - 1),
          )
        ]?.timestamp,
    });
    const cancel = () => {
      setDrawingInput(null);
      current.current.onDrawEnd();
    };
    const callbacks = () => ({
      onDrawEnd: () => {
        setDrawingInput(null);
        current.current.onDrawEnd();
      },
      onSelected: ({ overlay }: { overlay: { id: string } }) => {
        selected.current = overlay.id;
        current.current.onSelection(true);
      },
      onDeselected: () => {
        selected.current = null;
        current.current.onSelection(false);
      },
    });
    useImperativeHandle(ref, () => ({
      cancel,
      draw(name, text) {
        cancel();
        setDrawingInput({ name, text });
      },
      removeSelected() {
        if (!selected.current) return false;
        chartRef.current?.removeOverlay({ id: selected.current });
        selected.current = null;
        current.current.onSelection(false);
        return true;
      },
      zoom(scale) {
        chartRef.current?.zoomAtCoordinate(scale);
      },
      reset() {
        const c = chartRef.current;
        if (c) {
          c.setBarSpace(
            Math.min(
              50,
              Math.max(
                1,
                ((element.current?.clientWidth ?? 800) - 76) /
                  Math.min(200, Math.max(1, c.getDataList().length)),
              ),
            ),
          );
          c.scrollToRealTime();
        }
      },
    }));
    useLayoutEffect(() => {
      const c = init(element.current!, options);
      if (!c) return;
      chartRef.current = c;
      c.setMaxOffsetLeftDistance(0);
      c.setMaxOffsetRightDistance(0);
      c.setOffsetRightDistance(0);
      c.overrideXAxis({ scrollZoomEnabled: false });
      c.setSymbol({
        ticker: current.current.code,
        pricePrecision: 3,
        volumePrecision: 2,
      });
      c.setPeriod({ type: "day", span: 1 });
      const inspect = (value: unknown) => {
        const index = (value as Partial<Crosshair>).dataIndex;
        current.current.onInspect(
          index === undefined ? null : (current.current.bars[index] ?? null),
        );
      };
      const range = () => {
        const r = c.getVisibleRange(),
          data = current.current.bars;
        const a = Math.max(0, Math.floor(r.from)),
          b = Math.min(data.length - 1, Math.ceil(r.to) - 1);
        current.current.onRange(
          data[a] && data[b]
            ? `${data[a].date.replaceAll("-", "/")} — ${data[b].date.replaceAll("-", "/")} · ${b - a + 1} 根 K 线`
            : "暂无可见行情",
        );
      };
      c.subscribeAction("onCrosshairChange", inspect);
      c.subscribeAction("onVisibleRangeChange", range);
      const observer = new ResizeObserver(() => c.resize());
      observer.observe(element.current!);
      return () => {
        observer.disconnect();
        dispose(c);
        chartRef.current = null;
        previous.current = { code: "", basis: "", period: "", replay: false };
      };
    }, []);
    useEffect(() => {
      const c = chartRef.current;
      if (!c) return;
      cancel();
      const old = previous.current,
        saved = c.getDataList().length
          ? view(c)
          : (lastValidView.current ?? view(c)),
        key = `${props.code}:${props.basis}`;
      if (c.getDataList().length) lastValidView.current = saved;
      if (old.code !== props.code || old.basis !== props.basis) {
        if (old.code)
          drawings.current.set(
            `${old.code}:${old.basis}`,
            c.getOverlays().map((o) => ({
              name: o.name,
              points: o.points.map((p) => ({ ...p })),
              extendData: o.extendData,
            })),
          );
        c.removeOverlay();
        selected.current = null;
        current.current.onSelection(false);
        for (const o of drawings.current.get(key) ?? [])
          c.createOverlay({ ...o, ...callbacks() });
        if (old.code !== props.code) {
          beforeReplay.current = null;
          lastValidView.current = null;
        }
      }
      if (props.replay && !old.replay) beforeReplay.current = saved;
      if (old.code !== props.code)
        c.setSymbol({
          ticker: props.code,
          pricePrecision: 3,
          volumePrecision: 2,
        });
      if (old.period !== props.period)
        c.setPeriod({ type: props.period, span: 1 });
      c.setDataLoader({
        getBars: ({ callback }) =>
          callback(
            props.bars.map((bar) => ({ ...bar })),
            false,
          ),
      });
      // Drawings with anchors beyond the cutoff would expose future prices.
      for (const o of c.getOverlays())
        c.overrideOverlay({
          id: o.id,
          visible:
            !props.replay ||
            o.points.every(
              (p) =>
                !p.timestamp ||
                p.timestamp <= (props.bars.at(-1)?.timestamp ?? 0),
            ),
        });
      const frame = requestAnimationFrame(() => {
        // The chart library cannot binary-search an empty series. Preserve the
        // last valid view while a missing-factor state has no chart data.
        if (!c.getDataList().length) return;
        const restore =
          old.replay && !props.replay ? beforeReplay.current : saved;
        if (old.code !== props.code || !restore?.timestamp) {
          c.setBarSpace(
            Math.min(
              50,
              Math.max(
                1,
                ((element.current?.clientWidth ?? 800) - 76) /
                  Math.min(200, Math.max(props.bars.length, 1)),
              ),
            ),
          );
          c.scrollToRealTime();
        } else {
          c.setBarSpace(restore.space);
          if (props.replay) c.scrollToRealTime();
          else c.scrollToTimestamp(restore.timestamp);
        }
      });
      previous.current = {
        code: props.code,
        basis: props.basis,
        period: props.period,
        replay: props.replay,
      };
      return () => cancelAnimationFrame(frame);
    }, [props.bars, props.code, props.basis, props.period, props.replay]);
    useEffect(() => {
      const c = chartRef.current;
      if (!c) return;
      const settings = props.indicators;
      for (const name of ["MA", "VOL", "MACD", "RSI"] as const) {
        const enabled =
          settings[name] && (name !== "VOL" || !props.volumeMissing);
        const ma = settings.ma.filter((_, i) => settings.maVisible[i]);
        const config = {
          name,
          precision:
            name === "RSI" ? 2 : name === "VOL" ? 2 : name === "MACD" ? 4 : 3,
          ...(name === "RSI" ? { minValue: 0, maxValue: 100 } : {}),
          ...(name === "VOL" ? { shortName: "VOL · 手" } : {}),
          calcParams:
            name === "MA"
              ? ma
              : name === "VOL"
                ? [settings.vol]
                : name === "MACD"
                  ? settings.macd
                  : [settings.rsi],
          ...(name === "MA"
            ? {
                paneId: "candle_pane",
                styles: {
                  lines: MA_COLORS.filter((_, i) => settings.maVisible[i]).map(
                    (color) => ({ color, size: 1.5 }),
                  ),
                },
              }
            : {}),
        };
        if (!enabled || (name === "MA" && !ma.length))
          c.removeIndicator({ name });
        else if (c.getIndicators({ name }).length) c.overrideIndicator(config);
        else {
          const pane = c.createIndicator(config, name === "MA");
          if (pane && name !== "MA") {
            c.setPaneOptions({
              id: pane,
              height: 110,
              minHeight: 90,
              dragEnabled: true,
            });
            c.overrideYAxis({ paneId: pane, gap: { top: 0.6, bottom: 0.05 } });
          }
        }
      }
    }, [props.indicators, props.volumeMissing]);
    useEffect(() => {
      const c = chartRef.current;
      if (!c || (!props.range.start && !props.range.end)) return;
      const subset = props.bars.filter(
        (b) =>
          (!props.range.start || b.date >= props.range.start) &&
          (!props.range.end || b.date <= props.range.end),
      );
      if (!subset.length) return;
      const frame = requestAnimationFrame(() => {
        if (!c.getDataList().length) return;
        c.setBarSpace(
          Math.min(
            50,
            Math.max(
              1,
              ((element.current?.clientWidth ?? 800) - 76) / subset.length,
            ),
          ),
        );
        c.scrollToTimestamp(subset.at(-1)!.timestamp);
      });
      return () => cancelAnimationFrame(frame);
    }, [props.range, props.period, props.code]);
    return (
      <>
        <div
          ref={element}
          className="qfe-chart"
          tabIndex={0}
          role="img"
          aria-label="ETF K线与指标图。滚轮缩放，拖拽回看。聚焦后按左右方向键查看各根行情。"
          onMouseLeave={() => props.onInspect(null)}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              cancel();
              return;
            }
            if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
            event.preventDefault();
            const c = chartRef.current;
            if (!c || !props.bars.length) return;
            const index = Math.max(
              0,
              Math.min(
                props.bars.length - 1,
                Number(element.current?.dataset.index ?? props.bars.length) +
                  (event.key === "ArrowLeft" ? -1 : 1),
              ),
            );
            element.current!.dataset.index = String(index);
            const bar = props.bars[index];
            props.onInspect(bar);
            c.scrollToDataIndex(index);
          }}
        />
        {drawingInput && (
          <EtfDrawingInput
            key={drawingInput.name}
            chartRef={chartRef}
            mode={drawingInput}
            complete={(points) => {
              chartRef.current?.createOverlay({
                name: drawingInput.name,
                points,
                extendData: drawingInput.text,
                needDefaultPointFigure: true,
                ...callbacks(),
              });
              setDrawingInput(null);
              current.current.onDrawEnd();
            }}
          />
        )}
      </>
    );
  },
);
