import type { CalendarDay, MarketOverview } from "../../features/market-data/data";

export const MARKET_PATH = "/admin/data/etf-basics";
export const EXCHANGES = [{ key: "SSE", name: "上交所" }, { key: "SZSE", name: "深交所" }];
export const LIST_STATES = [["", "全部"], ["L", "上市"], ["D", "退市"], ["P", "待上市"]] as const;
export interface MarketFilters { keyword: string; exchange: string; listStatus: string; limit: number; offset: number }

/** Only accept supported filters and bounded integers from shareable URLs. */
export function marketFilters(params: URLSearchParams): MarketFilters {
  const limit = Number(params.get("limit") ?? 50);
  const offset = Number(params.get("offset") ?? 0);
  const pageSize = [20, 50, 100].includes(limit) ? limit : 50;
  return {
    keyword: (params.get("keyword") ?? "").slice(0, 128),
    exchange: EXCHANGES.some(item => item.key === params.get("exchange")) ? params.get("exchange")! : "",
    listStatus: ["L", "D", "P"].includes(params.get("status") ?? "") ? params.get("status")! : "",
    limit: pageSize,
    offset: Number.isSafeInteger(offset) && offset >= 0 && offset <= 10_000_000 ? Math.floor(offset / pageSize) * pageSize : 0
  };
}

export function marketQuery(filters: MarketFilters): string {
  const params = new URLSearchParams();
  if (filters.keyword.trim()) params.set("keyword", filters.keyword.trim());
  if (filters.exchange) params.set("exchange", filters.exchange);
  if (filters.listStatus) params.set("status", filters.listStatus);
  if (filters.limit !== 50) params.set("limit", String(filters.limit));
  if (filters.offset) params.set("offset", String(filters.offset));
  return params.toString();
}

/** A detail page may restore only the originating market route, never an
 * arbitrary destination supplied by history state or an external URL. */
export function marketReturnTo(value: unknown): string {
  if (typeof value !== "string" || (value !== MARKET_PATH && !value.startsWith(`${MARKET_PATH}?`))) return MARKET_PATH;
  const query = marketQuery(marketFilters(new URLSearchParams(value.split("?")[1])));
  return MARKET_PATH + (query ? `?${query}` : "");
}

export function exchangeName(value: string | null): string {
  if (value === null) return "未知";
  return ({ SSE: "上交所", SH: "上交所", SZSE: "深交所", SZ: "深交所" } as Record<string, string>)[value] ?? value;
}
export function statusName(value: string | null): string {
  return LIST_STATES.find(([key]) => key === value)?.[1] ?? "未知状态";
}
export function marketDate(value: string | null | undefined): string { return value ? value.replaceAll("-", "/") : "—"; }
export function marketTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(date);
}
export function managementFee(value: string | null, unit: string | null = null): string {
  if (value === null || !/^-?(0|[1-9][0-9]*)(\.[0-9]+)?$/.test(value)) return "—";
  return value + (unit ? ` ${unit}` : "");
}
export function syncPresentation(overview: MarketOverview | null) {
  return { label: "读取时点", value: marketTime(overview?.meta.read_at),
    note: overview ? "当前修订资料 · 不是采集更新时间" : "等待读取" };
}

export function shanghaiToday(now = new Date()): string {
  const parts = new Intl.DateTimeFormat("en-US", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(now);
  const part = (type: string) => parts.find(item => item.type === type)!.value;
  return `${part("year")}-${part("month")}-${part("day")}`;
}

/** UTC is used solely for date geometry; trading status always comes from
 * persisted exchange records, including holidays and missing coverage. */
export function calendarDates(month: string): string[] {
  const first = new Date(`${month}-01T00:00:00Z`);
  first.setUTCDate(1 - (first.getUTCDay() + 6) % 7);
  return Array.from({ length: 42 }, (_, index) => new Date(first.getTime() + index * 86_400_000).toISOString().slice(0, 10));
}
export function shiftMonth(month: string, delta: number): string {
  const date = new Date(`${month}-01T00:00:00Z`);
  date.setUTCMonth(date.getUTCMonth() + delta);
  return date.toISOString().slice(0, 7);
}
export function calendarState(day: CalendarDay | null | undefined): "open" | "closed" | "unknown" {
  return day?.state ?? "unknown";
}
export const CALENDAR_LABELS = { open: "交易日", closed: "休市", unknown: "未知" };

export function filterDescription(filters: MarketFilters): string {
  return [filters.keyword.trim() ? `搜索“${filters.keyword.trim()}”` : "全部关键词", filters.exchange ? exchangeName(filters.exchange) : "全部交易所", filters.listStatus ? statusName(filters.listStatus) : "全部状态"].join(" · ");
}
