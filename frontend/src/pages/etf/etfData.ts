import type { MarketBar } from "../../features/market-data/data";

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
  exact?: MarketBar;
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

export function validParams(key: string, values: number[]): boolean {
  return (
    values.length === ({ MA: 4, MACD: 3, RSI: 1, VOL: 1 }[key] ?? 0) &&
    values.length > 0 &&
    values.every((v) => Number.isInteger(v) && v >= 1 && v <= 250) &&
    (key !== "MACD" || values[0] < values[1])
  );
}
