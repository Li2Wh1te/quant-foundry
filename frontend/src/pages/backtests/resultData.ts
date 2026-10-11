import type { ResultRow, ResultSummary } from "../../api/backtestResults";
export type { ResultRow } from "../../api/backtestResults";
export const RESULT_KINDS = { equity: "权益", orders: "订单", trades: "成交与费用", positions: "持仓", records: "用户 record", logs: "日志" };
export const METRICS = [
  { key: "total_return", rust: "cumulative_return", name: "累计收益", percent: true },
  { key: "annualized_return", rust: "annualized_return", name: "年化收益", percent: true },
  { key: "max_drawdown", rust: "maximum_drawdown", name: "最大回撤", percent: true },
  { key: "sharpe", rust: "sharpe", name: "夏普比率", percent: false },
  { key: "volatility", rust: "volatility", name: "年化波动率", percent: true },
  { key: "turnover", rust: "turnover", name: "换手率", percent: false },
  { key: "benchmark_return", rust: "benchmark_return", name: "基准收益", percent: true },
  { key: "cumulative_excess_return", rust: "cumulative_excess_return", name: "累计超额", percent: true },
  { key: "cumulative_fees", rust: "fill_fees", name: "成交费用", percent: false },
];
export function metricValue(value: unknown): string | number | null {
  if (value && typeof value === "object") { const metric = value as ResultRow; return metric.status === "available" ? metricValue(metric.value) : null; }
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  return typeof value === "string" && /^-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)$/.test(value) ? value : null;
}
export function numeric(value: unknown): number | null { const v = metricValue(value); return v === null || !Number.isFinite(Number(v)) ? null : Number(v); }
export function reason(value: unknown): string { return value && typeof value === "object" && typeof (value as ResultRow).reason === "string" ? (value as ResultRow).reason : "未保存有效计算值"; }
function percentString(value: string): string {
  const negative = value.startsWith("-"), [whole, fraction = ""] = value.replace(/^-/, "").split(".");
  const integer = ((whole || "0") + fraction.padEnd(2, "0").slice(0, 2)).replace(/^0+(?=\d)/, "");
  const decimals = fraction.slice(2).padEnd(2, "0"), nonzero = /[1-9]/.test(integer + decimals);
  return `${negative ? "-" : nonzero ? "+" : ""}${integer}.${decimals}%`;
}
/** Raw saved Decimal strings are never round-tripped through Number. */
export function displayNumber(value: unknown, percent = false): string {
  const raw = metricValue(value); if (raw === null) return "—";
  if (typeof raw === "string") return percent ? percentString(raw) : raw;
  return percent ? `${raw > 0 ? "+" : ""}${(raw * 100).toFixed(2)}%` : String(raw);
}
export function exactDifference(a: unknown, b: unknown): string | null {
  const left = metricValue(a), right = metricValue(b); if (left === null || right === null) return null;
  if (typeof left !== "string" || typeof right !== "string") return String(Number(left) - Number(right));
  const parts = (v: string) => { const [i, f = ""] = v.split("."); return { i, f }; };
  const x = parts(left), y = parts(right), scale = Math.max(x.f.length, y.f.length);
  const integer = (v: ReturnType<typeof parts>) => BigInt((v.i.replace(/^-/, "") || "0") + v.f.padEnd(scale, "0")) * (v.i.startsWith("-") ? -1n : 1n);
  const delta = integer(x) - integer(y), digits = (delta < 0 ? -delta : delta).toString().padStart(scale + 1, "0");
  return `${delta < 0 ? "-" : ""}${scale ? digits.slice(0, -scale) + "." + digits.slice(-scale) : digits}`;
}
export function localDay(value: unknown, timezone = "Asia/Shanghai"): string {
  const date = new Date(typeof value === "number" ? value : String(value));
  if (!Number.isFinite(date.getTime())) return "";
  return new Intl.DateTimeFormat("sv-SE", { timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit" }).format(date);
}
export function timeISO(value: unknown): string {
  try { if (typeof value !== "string" || !/^-?[0-9]+$/.test(value)) return ""; return new Date(Number(BigInt(value) / 1000000n)).toISOString(); } catch { return ""; }
}
export function chartRows(rows: ResultRow[]): ResultRow[] {
  return rows.map(row => ({ ...row, as_of: row.as_of || timeISO(row.time_ns), period_return: row.session_return ?? row.period_return,
    valuation_status: row.equity === null || row.unavailable_reason ? "blocked" : row.valuation_status,
    valuation_reason: row.unavailable_reason || row.valuation_reason }));
}
export function summaryMetrics(summary: ResultSummary | null | undefined): ResultRow[] {
  if (!summary) return [];
  if (summary.schema_id !== "legacy") {
    const p = summary.performance; if (!p) return [];
    return METRICS.map(m => { const v = m.key === "cumulative_fees" ? p.trades?.fill_fees : p[m.rust]; return { metric_key: m.key, value: metricValue(v), unavailable_reason: reason(v), formula_version: p.policy?.formula_id, policy: p.policy }; });
  }
  return Array.isArray(summary.metrics) ? summary.metrics : [];
}
