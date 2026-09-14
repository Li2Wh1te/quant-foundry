import type { ComparisonRow } from "../../api/backtestComparisons";
import { finite } from "./workbench";
export const COMPARISON_METRICS = [
  { key: "total_return", name: "总收益", percent: true, direction: "max" },
  { key: "annualized_return", name: "年化收益", percent: true, direction: "max" },
  { key: "max_drawdown", name: "最大回撤", percent: true, direction: "minAbs" },
  { key: "sharpe", name: "夏普比率", percent: false, direction: "max" },
  { key: "volatility", name: "年化波动率", percent: true, direction: "minAbs" },
  { key: "turnover", name: "换手率", percent: false, direction: "" },
  { key: "cumulative_fees", name: "累计费用", percent: false, direction: "" },
];
export function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => `${JSON.stringify(k)}:${canonical(v)}`).join(",")}}`;
  return JSON.stringify(value) ?? "undefined";
}
export const comparisonConfig = (run: ComparisonRow) => run.backtest_config?.spec || run.backtest_config || {};
export function selectedMetric(metrics: ComparisonRow[], id: string, key: string): ComparisonRow | undefined {
  const rows = metrics.filter(row => row.run_id === id && row.metric_key === key);
  return rows.length === 1 ? rows[0] : undefined;
}
/** Equal numbers do not imply comparable evidence. Missing conventions cannot
 * establish equality, and full-run metrics must never imply a clipped period. */
export function comparisonReason(a: ComparisonRow, b: ComparisonRow, left?: ComparisonRow, right?: ComparisonRow): string {
  const ac = comparisonConfig(a), bc = comparisonConfig(b);
  if (!ac.start_date || !ac.end_date || !bc.start_date || !bc.end_date) return "回测区间未记录";
  if (ac.start_date !== bc.start_date || ac.end_date !== bc.end_date) return "回测区间不同";
  if (!left || !right) return "指标未产出或存在多个计算版本";
  if (finite(left.value) === null || finite(right.value) === null) return "指标不可计算";
  for (const row of [left, right]) if (!row.formula_version || !row.analyzer_key || row.analyzer_version == null || !row.unit) return "计算口径记录不完整";
  for (const row of [left, right]) {
    if (!Number.isInteger(row.sample_count) || row.sample_count < 0) return "样本口径未记录";
    if (["annualized_return", "volatility", "sharpe"].includes(row.metric_key) && finite(row.annualization_factor) === null) return "年化口径未记录";
    if (row.metric_key === "sharpe" && !row.risk_free_rate_note) return "无风险利率口径未记录";
  }
  const keys = ["formula_version", "analyzer_key", "analyzer_version", "unit", "annualization_factor", "risk_free_rate_note", "analyzer_metadata", "sample_count"];
  if (keys.some(key => canonical(left[key]) !== canonical(right[key]))) return "计算口径或样本不同";
  if (["currency", "timezone", "frequency"].some(key => !ac[key] || !bc[key] || ac[key] !== bc[key])) return "币种、时区或频率不一致/未记录";
  return "";
}
export function metricDifference(a: unknown, b: unknown, percent: boolean): string {
  const left = finite(a), right = finite(b); if (left === null || right === null) return "—";
  const delta = (left - right) * (percent ? 100 : 1);
  return `${delta > 0 ? "+" : ""}${delta.toFixed(percent ? 2 : 4)}${percent ? " 个百分点" : ""}`;
}
export function comparisonUrl(ids: string[], baseline = ids[0] || "", saved = "") {
  const query = new URLSearchParams(); if (ids.length || saved) query.set("runs", ids.join(",")); if (baseline) query.set("baseline", baseline); if (saved) query.set("saved", saved);
  return `/admin/backtest-compare${query.size ? `?${query}` : ""}`;
}
export const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export function parseSelection(query: URLSearchParams) {
  const raw = query.get("runs"); const ids = raw ? raw.split(",") : [];
  const baseline = query.get("baseline") || ids[0] || "";
  const error = ids.length > 10 || new Set(ids).size !== ids.length || ids.some(id => !UUID_PATTERN.test(id)) || baseline && !ids.includes(baseline) ? "对比链接包含无效、重复或超过10个运行，请重新选择。" : "";
  return { ids, baseline, error };
}
