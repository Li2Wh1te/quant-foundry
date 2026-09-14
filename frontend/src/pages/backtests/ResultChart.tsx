import { useEffect, useMemo, useRef, useState } from "react";
import { finite } from "./workbench";
import { displayNumber, localDay, type ResultRow } from "./resultData";
/** Keep missing samples in the path sequence. Pointer and keyboard inspection
 * use the original data, even when many points share the same screen pixel. */
export function ResultChart({ rows, field, title, bars = false, percent = true, timezone }: { rows: ResultRow[]; field: string; title: string; bars?: boolean; percent?: boolean; timezone: string }) {
  const [chosen, setChosen] = useState(0), [width, setWidth] = useState(800);
  const container = useRef<HTMLDivElement>(null);
  useEffect(() => { const node = container.current; if (!node) return; const observer = new ResizeObserver(entries => setWidth(Math.max(180, entries[0].contentRect.width))); observer.observe(node); return () => observer.disconnect(); }, [rows.length]);
  const points = useMemo(() => rows.map(row => ({ row, x: Date.parse(row.as_of), y: row.valuation_status === "blocked" ? null : finite(row[field]) })), [rows, field]);
  const valid = points.filter(p => Number.isFinite(p.x) && p.y !== null);
  if (!valid.length) return <div ref={container} className="qfr-chart-empty">暂无可绘制的{title}数据</div>;
  const [xmin, xmax, rawmin, rawmax] = valid.reduce((b, p) => [Math.min(b[0], p.x), Math.max(b[1], p.x), Math.min(b[2], p.y!), Math.max(b[3], p.y!)], [Infinity, -Infinity, 0, 0]);
  const padding = (rawmax - rawmin || .01) * .08, ymin = rawmin - padding, ymax = rawmax + padding;
  const x = (v: number) => 68 + (width - 80) * (v - xmin) / (xmax - xmin || 1), y = (v: number) => 265 - 230 * (v - ymin) / (ymax - ymin);
  let connected = false;
  const path = points.map(p => {
    if (p.y === null || !Number.isFinite(p.x)) { connected = false; return ""; }
    const command = `${connected ? "L" : "M"}${x(p.x)},${y(p.y)}`; connected = true; return command;
  }).join(" ");
  const selected = points[Math.min(chosen, points.length - 1)];
  return <div ref={container} className="qfr-chart-wrap">
    <div className="qfr-chart-readout" aria-live="polite"><span>{localDay(selected?.row.as_of, timezone)}</span><strong>{displayNumber(selected?.y, percent)}</strong>{selected?.y === null && <span>{selected.row.valuation_reason || "估值或收益缺失"}</span>}</div>
    <svg viewBox={`0 0 ${width} 305`} role="img" aria-label={`${title}，可使用下方日期滑块查看各时点数值`} onPointerMove={e => {
      const rect = e.currentTarget.getBoundingClientRect(), value = xmin + ((e.clientX - rect.left) / rect.width * width - 68) / (width - 80) * (xmax - xmin);
      let index = 0, distance = Infinity; points.forEach((p, i) => { if (Math.abs(p.x - value) < distance) { index = i; distance = Math.abs(p.x - value); } }); setChosen(index);
    }}>
      {[0, .25, .5, .75, 1].map(f => <g key={f}><line x1="68" x2={width - 12} y1={35 + f * 230} y2={35 + f * 230} stroke="var(--border)" strokeDasharray="3 5"/><text x="60" textAnchor="end" y={40 + f * 230}>{displayNumber(ymax - f * (ymax - ymin), percent)}</text></g>)}
      <line x1="68" x2={width - 12} y1={y(0)} y2={y(0)} stroke="var(--muted)" strokeOpacity=".45"/>
      {bars ? valid.map((p, i) => <line key={i} x1={x(p.x)} x2={x(p.x)} y1={y(0)} y2={y(p.y!)} stroke={p.y! >= 0 ? "var(--market-up)" : "var(--market-down)"} strokeWidth={Math.max(1, Math.min(8, 680 / valid.length))}/>) : <><path d={path} fill="none" stroke="var(--orange)" strokeWidth="2.2"/>{points.map((p, i) => p.y !== null && Number.isFinite(p.x) && (i === 0 || i === points.length - 1 || points[i - 1]?.y === null || points[i + 1]?.y === null) ? <circle key={i} cx={x(p.x)} cy={y(p.y!)} r="3" fill="var(--orange)"/> : null)}</>}
      {selected && Number.isFinite(selected.x) && <line x1={x(selected.x)} x2={x(selected.x)} y1="35" y2="265" stroke="var(--muted)" strokeDasharray="4 4"/>}
      <text x="68" y="295">{localDay(xmin, timezone)}</text><text x={width - 12} y="295" textAnchor="end">{localDay(xmax, timezone)}</text>
    </svg>
    <label className="qfr-chart-slider">查看时点<input aria-label={`${title}查看时点`} type="range" min="0" max={points.length - 1} value={Math.min(chosen, points.length - 1)} onChange={e => setChosen(Number(e.target.value))}/></label>
  </div>;
}
