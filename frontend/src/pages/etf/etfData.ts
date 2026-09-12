import type {
  EtfAdjustmentFactor,
  EtfDailyBar,
} from "../../api/dataCollections";

export type Period = "day" | "week" | "month" | "year";
export type Adjustment = "raw" | "forward" | "backward";
export const PERIODS: Record<Period, string> = {
  day: "日",
  week: "周",
  month: "月",
  year: "年",
};
export const ADJUSTMENTS: Record<Adjustment, string> = {
  raw: "不复权",
  forward: "前复权",
  backward: "后复权",
};
export const MA_COLORS = ["#B68B3D", "#4F8F8B", "#4E7C9C", "#8B6596"];
export interface Bar {
  timestamp: number;
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
  turnover?: number;
}
export interface IndicatorSettings {
  MA: boolean;
  VOL: boolean;
  MACD: boolean;
  RSI: boolean;
  ma: number[];
  maVisible: boolean[];
  macd: number[];
  rsi: number;
  vol: number;
}
export const DEFAULT_INDICATORS: IndicatorSettings = {
  MA: true,
  VOL: true,
  MACD: false,
  RSI: false,
  ma: [5, 10, 20, 60],
  maVisible: [true, true, true, true],
  macd: [12, 26, 9],
  rsi: 14,
  vol: 5,
};

export function numeric(value: unknown): number | null {
  if (
    value === null ||
    value === undefined ||
    (typeof value !== "number" && typeof value !== "string") ||
    String(value).trim() === ""
  )
    return null;
  const result = Number(value);
  return Number.isFinite(result) ? result : null;
}
export function numberText(value: unknown, digits = 3): string {
  const n = numeric(value);
  return n === null
    ? "—"
    : n.toLocaleString("zh-CN", {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      });
}
export function signed(value: unknown, digits = 3): string {
  const n = numeric(value);
  return n === null ? "—" : `${n > 0 ? "+" : ""}${numberText(n, digits)}`;
}
export function direction(value: unknown): string {
  const n = numeric(value);
  return n === null || n === 0 ? "neutral" : n > 0 ? "up" : "down";
}
export const exchangeName = (value: string | null | undefined) =>
  ({ SH: "上交所", SSE: "上交所", SZ: "深交所", SZSE: "深交所" })[
    value ?? ""
  ] ??
  value ??
  "—";
export const dateText = (value: string | null | undefined) =>
  value?.replaceAll("-", "/") ?? "—";
export const timeText = (value: string | null | undefined) =>
  value
    ? new Intl.DateTimeFormat("zh-CN", {
        timeZone: "Asia/Shanghai",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }).format(new Date(value))
    : "—";

function bucket(date: string, period: Period): string {
  if (period === "day") return date;
  if (period === "month") return date.slice(0, 7);
  if (period === "year") return date.slice(0, 4);
  const d = new Date(`${date}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() - ((d.getUTCDay() + 6) % 7));
  return d.toISOString().slice(0, 10);
}

export function buildSeries(
  source: EtfDailyBar[],
  factors: EtfAdjustmentFactor[],
  adjustment: Adjustment,
  period: Period,
  cutoff?: string,
): { bars: Bar[]; error: string; volumeMissing: boolean } {
  // Truncate before adjustment and aggregation: replay must not use a future
  // factor as its anchor or include future sessions in a weekly/monthly bar.
  const rows = [...source]
    .filter((r) => !cutoff || r.trade_date <= cutoff)
    .sort((a, b) => a.trade_date.localeCompare(b.trade_date));
  const factorMap = new Map(
    factors.map((f) => [f.trade_date, numeric(f.adj_factor)]),
  );
  if (!rows.length) return { bars: [], error: "", volumeMissing: false };
  const invalid = rows.filter((r) => {
    const o = numeric(r.open),
      h = numeric(r.high),
      l = numeric(r.low),
      c = numeric(r.close);
    return (
      o === null ||
      h === null ||
      l === null ||
      c === null ||
      Math.min(o, h, l, c) <= 0 ||
      h < Math.max(o, c, l) ||
      l > Math.min(o, c)
    );
  });
  // Block an invalid price series rather than dropping dates and bridging a
  // gap with a plausible-looking moving average. Raw facts remain inspectable.
  if (invalid.length)
    return {
      bars: [],
      error: `${invalid.length} 条日线价格缺失或无效（首条 ${invalid[0].trade_date}），请核查采集数据后重试。`,
      volumeMissing: false,
    };
  const missing =
    adjustment === "raw"
      ? []
      : rows.filter(
          (r) =>
            !factorMap.get(r.trade_date) || factorMap.get(r.trade_date)! <= 0,
        );
  if (missing.length)
    return {
      bars: [],
      error: `${ADJUSTMENTS[adjustment]}缺少 ${missing.length} 个交易日的有效复权因子（${missing[0].trade_date} 至 ${missing.at(-1)!.trade_date}）。请补采，或切回不复权。`,
      volumeMissing: false,
    };
  const anchor =
    adjustment === "raw"
      ? 1
      : factorMap.get(
          (adjustment === "forward" ? rows.at(-1)! : rows[0]).trade_date,
        )!;
  const result: Bar[] = [];
  let lastBucket = "",
    volumeMissing = false;
  for (const row of rows) {
    const scale =
      adjustment === "raw" ? 1 : factorMap.get(row.trade_date)! / anchor;
    const volume = numeric(row.vol),
      turnover = numeric(row.amount);
    if (volume === null || volume < 0) volumeMissing = true;
    const bar: Bar = {
      timestamp: Date.parse(`${row.trade_date}T00:00:00Z`),
      date: row.trade_date,
      open: numeric(row.open)! * scale,
      high: numeric(row.high)! * scale,
      low: numeric(row.low)! * scale,
      close: numeric(row.close)! * scale,
      volume: volume === null || volume < 0 ? undefined : volume,
      turnover: turnover === null || turnover < 0 ? undefined : turnover,
    };
    const key = bucket(row.trade_date, period),
      prior = result.at(-1);
    if (prior && key === lastBucket) {
      prior.timestamp = bar.timestamp;
      prior.date = bar.date;
      prior.high = Math.max(prior.high, bar.high);
      prior.low = Math.min(prior.low, bar.low);
      prior.close = bar.close;
      prior.volume =
        prior.volume === undefined || bar.volume === undefined
          ? undefined
          : prior.volume + bar.volume;
      prior.turnover =
        prior.turnover === undefined || bar.turnover === undefined
          ? undefined
          : prior.turnover + bar.turnover;
    } else result.push(bar);
    lastBucket = key;
  }
  return { bars: result, error: "", volumeMissing };
}

export function validParams(key: string, values: number[]): boolean {
  return (
    values.length === ({ MA: 4, MACD: 3, RSI: 1, VOL: 1 }[key] ?? 0) &&
    values.length > 0 &&
    values.every((v) => Number.isInteger(v) && v >= 1 && v <= 250) &&
    (key !== "MACD" || values[0] < values[1])
  );
}
