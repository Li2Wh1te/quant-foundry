import { useRef, useState, type RefObject } from "react";
import type { Chart, Point } from "klinecharts";

export interface DrawingInput {
  name: string;
  text?: string;
}
// Collect anchors with native pointer events. KLineCharts treats two fast,
// separated clicks as a double click and drops the second anchor; this layer
// accepts both mouse and touch anchors without altering the library internals.
export function EtfDrawingInput({
  chartRef,
  mode,
  complete,
}: {
  chartRef: RefObject<Chart | null>;
  mode: DrawingInput;
  complete: (points: Partial<Point>[]) => void;
}) {
  const [points, setPoints] = useState<Partial<Point>[]>([]);
  const [hover, setHover] = useState<{ x: number; y: number } | null>(null);
  const down = useRef<{ x: number; y: number } | null>(null);
  const c = chartRef.current;
  const pixels = (c?.convertToPixel(points, { paneId: "candle_pane" }) ??
    []) as { x: number; y: number }[];
  const preview = [...pixels, ...(hover ? [hover] : [])];
  return (
    <svg
      className="qfe-drawing-input"
      aria-label="点击图表放置绘图锚点，Esc 取消"
      onPointerDown={(event) => {
        event.preventDefault();
        down.current = { x: event.clientX, y: event.clientY };
        event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerMove={(event) => {
        const box = event.currentTarget.getBoundingClientRect();
        setHover({ x: event.clientX - box.left, y: event.clientY - box.top });
      }}
      onPointerUp={(event) => {
        const start = down.current;
        down.current = null;
        if (
          !start ||
          Math.hypot(event.clientX - start.x, event.clientY - start.y) > 6 ||
          !c
        )
          return;
        const box = event.currentTarget.getBoundingClientRect(),
          x = event.clientX - box.left,
          y = event.clientY - box.top;
        const size = c.getSize("candle_pane", "main");
        if (!size || x < 0 || y < 0 || x > size.width || y > size.height)
          return;
        const converted = c.convertFromPixel([{ x, y }], {
          paneId: "candle_pane",
        });
        const point = Array.isArray(converted) ? converted[0] : converted;
        if (
          !point ||
          point.value === undefined ||
          !Number.isFinite(point.value)
        )
          return;
        const data = c.getDataList(),
          index = Math.max(
            0,
            Math.min(data.length - 1, Math.round(point.dataIndex ?? 0)),
          );
        if (!data[index]) return;
        const next = [
          ...points,
          { timestamp: data[index].timestamp, value: point.value },
        ];
        const count =
          mode.name === "simpleAnnotation"
            ? 1
            : mode.name === "parallelStraightLine"
              ? 3
              : 2;
        if (next.length === count) complete(next);
        else setPoints(next);
      }}
      onPointerCancel={() => {
        down.current = null;
      }}
    >
      <polyline
        points={preview.map((p) => `${p.x},${p.y}`).join(" ")}
        fill="none"
        stroke="#E85F32"
        strokeWidth="1.5"
        strokeDasharray="4 3"
      />
      {pixels.map((p, i) => (
        <circle
          key={i}
          cx={p.x}
          cy={p.y}
          r="4"
          fill="#141922"
          stroke="#E85F32"
          strokeWidth="1.5"
        />
      ))}
    </svg>
  );
}
