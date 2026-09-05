import { useState, type FormEvent } from "react";
import type { BacktestRunFilters as Filters } from "../api/backtestRuns";

/** Apply filters as one query so partial date/revision edits never issue requests. */
export function BacktestRunFilters({ onApply }: { onApply: (filters: Filters) => void }) {
  const [revision, setRevision] = useState("");
  const [status, setStatus] = useState("");
  const [after, setAfter] = useState("");
  const [before, setBefore] = useState("");
  const [summary, setSummary] = useState("");
  function submit(event: FormEvent) {
    event.preventDefault();
    if (after && before && after > before) return;
    onApply({ strategy_revision_id: revision || undefined, status: status || undefined,
      created_after: after ? new Date(after).toISOString() : undefined,
      created_before: before ? new Date(before).toISOString() : undefined,
      config_summary: summary || undefined });
  }
  return <form onSubmit={submit} aria-label="筛选回测运行">
    <label>发布版本 ID<input value={revision} onChange={event => setRevision(event.target.value)} /></label>
    <label>运行状态<select value={status} onChange={event => setStatus(event.target.value)}>
      <option value="">全部状态</option>
      {Object.entries({queued:"排队中", starting:"启动中", running:"运行中", cancel_requested:"取消处理中", succeeded:"成功", failed:"失败", cancelled:"已取消", timed_out:"已超时", indeterminate:"结果待判定"}).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
    </select></label>
    <label>创建时间起<input type="datetime-local" value={after} max={before || undefined} onChange={event => setAfter(event.target.value)} /></label>
    <label>创建时间止<input type="datetime-local" value={before} min={after || undefined} onChange={event => setBefore(event.target.value)} /></label>
    <label>配置摘要<input value={summary} onChange={event => setSummary(event.target.value)} placeholder="日期、资金或配置内容" /></label>
    <button type="submit">筛选</button>
    <button type="button" onClick={() => { setRevision(""); setStatus(""); setAfter(""); setBefore(""); setSummary(""); onApply({}); }}>清除筛选</button>
  </form>;
}
