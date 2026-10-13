import type { BasisControlsProps } from "../series/basisSlot";
import type { Adjustment } from "../data";
import "./basis.css";

const labels: Record<Adjustment, string> = { none: "不复权", pre: "前复权", post: "后复权" };
export function BasisControls({ series, adjustment, asOf, disabled, onChange }: BasisControlsProps) {
  const capability = series?.adjustment_capabilities[adjustment];
  return <div className="qfb-basis" aria-label="价格口径">
    <label htmlFor="qfb-price-basis">价格口径</label>
    <select id="qfb-price-basis" value={adjustment} disabled={disabled} onChange={event => onChange(event.target.value as Adjustment)}>
      {(["none", "pre", "post"] as const).map(mode => {
        const supported = mode === "none" || series?.adjustment_capabilities[mode]?.availability === "available";
        return <option key={mode} value={mode} disabled={!supported}>{labels[mode]}{supported ? "" : " · 不可用"}</option>;
      })}
    </select>
    <details className="qfb-basis-note" onKeyDown={event => {
      if (event.key === "Escape") { event.currentTarget.open = false; event.currentTarget.querySelector("summary")?.focus(); }
    }}><summary>口径说明</summary>
      <p>当前修订数据 · 固定截止 {asOf}。改变口径或截止会重新读取整个窗口。</p>
      <p>{capability?.limitations.map(item => item.message).join(" ") || "所选口径仍须核验具体标的、日期和依赖。"}</p>
      {(["pre", "post"] as const).map(mode => <p key={mode}>{labels[mode]}：{series?.adjustment_capabilities[mode]?.limitations.map(item => item.message).join(" ") || "缺少可核验的因子政策与基准。"}</p>)}
      <button type="button" onClick={event => {
        const details = event.currentTarget.closest("details"); if (details) { details.open = false; details.querySelector("summary")?.focus(); }
      }}>收起说明</button>
    </details>
  </div>;
}
