import { readApiToken } from "../auth/tokenStorage";
import type { MarketOverview, MarketReadState } from "../features/market-data/data";
import { invalidateDataAssetsSession, registerRequest } from "../features/data-assets/data/lifecycle";

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
  market: MarketReadState<MarketOverview>;
}

export class OverviewApiError extends Error {
  constructor(readonly status: number) {
    super(status === 0 ? "运营服务连接失败。" : `运营请求失败（HTTP ${status}）`);
    this.name = "OverviewApiError";
  }
}

/** Operations facts remain separate from the page-owned formal market read. */
export async function getOperationsOverview(signal: AbortSignal): Promise<OperationsOverview> {
  signal.throwIfAborted();
  const token = readApiToken(), controller = new AbortController();
  const unregister = registerRequest(controller), abort = () => controller.abort();
  signal.addEventListener("abort", abort, { once: true });
  let timedOut = false;
  const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 20000);
  try {
    const response = await fetch("/api/admin/overview", {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      signal: controller.signal, cache: "no-store"
    });
    if ([401, 403].includes(response.status)) {
      invalidateDataAssetsSession(response.status === 401 ? "authentication" : "permission", controller);
      throw new OverviewApiError(response.status);
    }
    signal.throwIfAborted();
    if (readApiToken() !== token) throw new DOMException("Credential changed", "AbortError");
    if (!response.ok) throw new OverviewApiError(response.status);
    const result = await response.json();
    signal.throwIfAborted();
    if (readApiToken() !== token) throw new DOMException("Credential changed", "AbortError");
    return result as OperationsOverview;
  } catch (error) {
    if (error instanceof OverviewApiError) throw error;
    signal.throwIfAborted();
    if (timedOut) throw new OverviewApiError(504);
    throw new OverviewApiError(error instanceof TypeError ? 0 : 502);
  } finally {
    clearTimeout(timeout); signal.removeEventListener("abort", abort); unregister();
  }
}
