import { readApiToken } from "../auth/tokenStorage";
import type { EtfOverview, TradingCalendarOverview } from "./dataCollections";

export interface OverviewRun {
  id: string;
  task_id: string;
  task_name: string;
  task_type: string;
  task_type_name: string;
  task_type_english_name: string;
  source_key: string;
  status: "queued" | "running" | "succeeded" | "failed" | "skipped" | "interrupted" | "cancelled" | "timed_out" | "indeterminate";
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
}

export interface OperationsOverview {
  generated_at: string;
  timezone: "Asia/Shanghai";
  day_start: string;
  day_end: string;
  metrics: {
    configured_sources: number; total_sources: number; active_tasks: number;
    queued_runs: number; running_runs: number; today_runs: number;
    today_succeeded: number; attention_tasks: number;
  };
  sources: Array<{
    key: string; name: string; configured: boolean;
    connection_status: "not_checked"; active_tasks: number; last_success_at: string | null;
  }>;
  recent_runs: OverviewRun[];
  attention: Array<{ run: OverviewRun; retrying: boolean }>;
}

export interface OverviewSnapshot {
  operations: OperationsOverview | null;
  etfs: EtfOverview | null;
  calendar: TradingCalendarOverview | null;
}

export class OverviewApiError extends Error {
  constructor(readonly status: number) {
    // Never promote a raw proxy response or backend exception to operator copy.
    super(`请求失败（HTTP ${status}）`);
    this.name = "OverviewApiError";
  }
}

async function request<T>(path: string, signal: AbortSignal): Promise<T> {
  const token = readApiToken();
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener("abort", abort, { once: true });
  if (signal.aborted) abort();
  // A slow asset service must not discard successful scheduler responses.
  const timeout = setTimeout(abort, 20000);
  try {
    const response = await fetch(path, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      signal: controller.signal,
      cache: "no-store"
    });
    if (!response.ok) throw new OverviewApiError(response.status);
    return await response.json() as T;
  } finally {
    clearTimeout(timeout);
    signal.removeEventListener("abort", abort);
  }
}

/** Each region retains its last successful value independently. The scheduler
 * snapshot is server-owned; asset counts come from their existing read APIs and
 * are deliberately not presented as part of the same database snapshot. */
export async function loadOverviewSnapshot(previous: OverviewSnapshot, signal: AbortSignal) {
  const results = await Promise.allSettled([
    request<OperationsOverview>("/api/admin/overview", signal),
    request<EtfOverview>("/api/admin/data-collections/etfs/overview", signal),
    request<TradingCalendarOverview>("/api/admin/data-collections/trading-calendar/overview", signal)
  ]);
  if (signal.aborted) throw new DOMException("Request aborted", "AbortError");
  for (const result of results) {
    if (result.status === "rejected" && result.reason instanceof OverviewApiError && result.reason.status === 401) {
      throw result.reason;
    }
  }
  const [operations, etfs, calendar] = results;
  const labels = ["运营指标、数据源与运行记录", "ETF 资产", "交易日历资产"];
  return {
    snapshot: {
      operations: operations.status === "fulfilled" ? operations.value : previous.operations,
      etfs: etfs.status === "fulfilled" ? etfs.value : previous.etfs,
      calendar: calendar.status === "fulfilled" ? calendar.value : previous.calendar
    },
    errors: results.flatMap((result, index) => result.status === "rejected" ? [labels[index]] : [])
  };
}
