import { useEffect, useRef, useState } from "react";
import type { ComparisonRow } from "../../api/backtestComparisons";
import { displayNumber, localDay } from "./resultData";
import { finite } from "./workbench";
export interface DisplayRun { id: string; label: string; color: string; slot: string; }
function useWidth() {
  const ref = useRef<HTMLDivElement>(null), [width, setWidth] = useState(700);
  useEffect(() => { if (!ref.current) return; const observer = new ResizeObserver(entries => setWidth(Math.max(200, entries[0].contentRect.width))); observer.observe(ref.current); return () => observer.disconnect(); }, []);
  return { ref, width };
}
/** Union timestamps share one axis; absent or blocked observations break paths.
 * No forward fill, rebasing, or sample interpolation changes result meaning. */
export function ComparisonChart({ runs, series, field, title }: { runs: DisplayRun[]; series: { run_id: string; points: ComparisonRow[] }[]; field: string; title: string }) {
  const { ref, width } = useWidth(), [chosen, setChosen] = useState(0);
  const times = [...new Set(series.flatMap(s => s.points.map(p => Date.parse(p.as_of)).filter(Number.isFinite)))].sort((a, b) => a - b);
  const parsed = runs.map(run => { const lookup = new Map((series.find(s => s.run_id === run.id)?.points || []).map(p => [Date.parse(p.as_of), p])); return { ...run, points: times.map(x => { const row = lookup.get(x); return { x, y: row?.valuation_status === "blocked" ? null : finite(row?.[field]), reason: row?.valuation_reason || "该时点无有效数据" }; }) }; });
  const values = parsed.flatMap(s => s.points).filter(p => p.y !== null);
  const [min, max] = values.reduce((b, p) => [Math.min(b[0], p.y!), Math.max(b[1], p.y!)], [0, 0]);
  const pad = (max - min || .01) * .1, bottom = min - pad, top = max + pad;
  const x = (time: number) => 70 + (width - 88) * (time - times[0]) / ((times.at(-1)! - times[0]) || 1), y = (v: number) => 248 - 218 * (v - bottom) / (top - bottom);
  const index = Math.min(chosen, times.length - 1);
  return <div className="qcmp-plot" ref={ref}>
    <div className="qcmp-legend">{runs.map(run => <span key={run.id}><i style={{ background: run.color }}/>{run.slot} · {run.label}</span>)}</div>
    {!values.length ? <div className="qcmp-empty">暂无可绘制的{title}数据。</div> : <>
      <div className="qcmp-readout" aria-live="polite"><strong>{localDay(times[index])}</strong>{parsed.map(s => <span key={s.id}>{s.slot} {displayNumber(s.points[index]?.y, true)}{s.points[index]?.y === null && <small>（{s.points[index].reason}）</small>}</span>)}</div>
      <svg viewBox={`0 0 ${width} 286`} role="img" aria-label={`${title}，下方日期滑块支持键盘查看`} onPointerMove={e => { const rect = e.currentTarget.getBoundingClientRect(); const target = times[0] + ((e.clientX - rect.left) / rect.width * width - 70) / (width - 88) * (times.at(-1)! - times[0]); let distance = Infinity, nearest = 0; times.forEach((time, i) => { if (Math.abs(time - target) < distance) { distance = Math.abs(time - target); nearest = i; } }); setChosen(nearest); }}>
        {[0, .25, .5, .75, 1].map(f => <g key={f}><line x1="70" x2={width - 18} y1={30 + f * 218} y2={30 + f * 218} stroke="var(--border)" strokeDasharray="3 5"/><text x="62" y={34 + f * 218} textAnchor="end">{displayNumber(top - f * (top - bottom), true)}</text></g>)}
        <line x1="70" x2={width - 18} y1={y(0)} y2={y(0)} stroke="var(--muted)" strokeOpacity=".5"/>
        {parsed.map((s, si) => { let connected = false; const path = s.points.map(p => { if (p.y === null) { connected = false; return ""; } const d = `${connected ? "L" : "M"}${x(p.x)},${y(p.y)}`; connected = true; return d; }).join(" "); return <g key={s.id}><path d={path} fill="none" stroke={s.color} strokeWidth="2" strokeDasharray={si >= 5 ? "5 3" : undefined}/>{s.points.map((p, i) => p.y !== null && (i === 0 || i === s.points.length - 1 || s.points[i-1]?.y === null || s.points[i+1]?.y === null) ? <circle key={i} cx={x(p.x)} cy={y(p.y)} r="3" fill={s.color}/> : null)}</g>; })}
        <line x1={x(times[index])} x2={x(times[index])} y1="30" y2="248" stroke="var(--muted)" strokeDasharray="4 4"/>
        <text x="70" y="277">{localDay(times[0])}</text><text x={width - 18} y="277" textAnchor="end">{localDay(times.at(-1))}</text>
      </svg>
      <label className="qcmp-slider">查看日期<input type="range" aria-label={`${title}查看日期`} min="0" max={times.length - 1} value={index} onChange={e => setChosen(Number(e.target.value))}/></label>
    </>}
  </div>;
}
export function RiskScatter({ runs, metrics }: { runs: DisplayRun[]; metrics: ComparisonRow[] }) {
  const { ref, width } = useWidth();
  const points = runs.map(run => { const get = (key: string) => { const rows = metrics.filter(r => r.run_id === run.id && r.metric_key === key); return rows.length === 1 ? finite(rows[0].value) : null; }; return { ...run, x: get("volatility"), y: get("annualized_return") }; }).filter(p => p.x !== null && p.y !== null);
  const maxX = Math.max(.01, ...points.map(p => p.x!)) * 1.2, minY = Math.min(0, ...points.map(p => p.y!)), maxY = Math.max(.01, ...points.map(p => p.y!));
  const pad = (maxY - minY) * .15;
  const x = (v: number) => 70 + (width - 110) * v / maxX, y = (v: number) => 225 - 185 * (v - minY + pad) / (maxY - minY + 2 * pad);
  return <div className="qcmp-plot" ref={ref}>{!points.length ? <div className="qcmp-empty">年化收益或年化波动率未产出，无法绘制散点。</div> : <svg viewBox={`0 0 ${width} 286`} role="img" aria-label="风险收益分布，横轴年化波动率、纵轴年化收益">
    {[0, .5, 1].map(f => <g key={f}><line x1="70" x2={width - 30} y1={40 + f * 185} y2={40 + f * 185} stroke="var(--border)"/><text x="62" y={44 + f * 185} textAnchor="end">{displayNumber(maxY + pad - f * (maxY - minY + 2 * pad), true)}</text><text x={x(f * maxX)} y="252" textAnchor="middle">{displayNumber(f * maxX, true)}</text></g>)}
    {points.map(p => <g key={p.id} tabIndex={0} aria-label={`${p.slot} ${p.label}，年化波动率${displayNumber(p.x, true)}，年化收益${displayNumber(p.y, true)}`}><circle cx={x(p.x!)} cy={y(p.y!)} r="6" fill={p.color}/><text x={x(p.x!) + 9} y={y(p.y!) - 8}>{p.slot}</text><title>{p.label} · 波动 {displayNumber(p.x, true)} · 收益 {displayNumber(p.y, true)}</title></g>)}<text x={width / 2} y="280" textAnchor="middle">年化波动率 →</text>
  </svg>}<p className="qcmp-note">纵轴为年化收益；每个点代表完整运行区间，区间或口径不同不代表可直接排名。</p></div>;
}
