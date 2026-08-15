import { readApiToken } from "../auth/tokenStorage";

export interface TradingCalendarDay {
  exchange: string;
  calendar_date: string;
  is_open: boolean;
  previous_trading_date: string | null;
  updated_at: string;
}

export interface TradingCalendarPage {
  items: TradingCalendarDay[];
  total: number;
  limit: number;
  offset: number;
}

export interface TradingCalendarOverview {
  total_records: number;
  exchange_count: number;
  open_day_count: number;
  start_date: string | null;
  end_date: string | null;
  last_updated_at: string | null;
  checkpoints: Record<string, string>;
}

export interface TradingCalendarFilters {
  exchange?: string;
  isOpen?: boolean;
  startDate?: string;
  endDate?: string;
  limit?: number;
  offset?: number;
}

export class DataCollectionApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "DataCollectionApiError";
  }
}

function headers(): HeadersInit {
  const token = readApiToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function request<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: headers() });
  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）。`;
    try {
      const body = await response.json() as { detail?: unknown };
      if (typeof body.detail === "string") message = body.detail;
    } catch {
      // Keep the HTTP fallback when a proxy or server sends a non-JSON response.
    }
    throw new DataCollectionApiError(message, response.status);
  }
  return response.json() as Promise<T>;
}

export function getTradingCalendarOverview(): Promise<TradingCalendarOverview> {
  return request<TradingCalendarOverview>("/api/admin/data-collections/trading-calendar/overview");
}

export function listTradingCalendarDays(filters: TradingCalendarFilters): Promise<TradingCalendarPage> {
  const params = new URLSearchParams();
  if (filters.exchange) params.set("exchange", filters.exchange);
  if (filters.isOpen !== undefined) params.set("is_open", String(filters.isOpen));
  if (filters.startDate) params.set("start_date", filters.startDate);
  if (filters.endDate) params.set("end_date", filters.endDate);
  params.set("limit", String(filters.limit ?? 50));
  params.set("offset", String(filters.offset ?? 0));
  return request<TradingCalendarPage>(`/api/admin/data-collections/trading-calendar?${params}`);
}
