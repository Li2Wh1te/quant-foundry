import { readApiToken } from "../auth/tokenStorage";
export type BacktestRun = {
  run_id: string;
  run_kind: string;
  status: string;
  terminal_status?: string | null;
  progress_ratio: number;
  current_trading_date?: string | null;
  current_step?: string | null;
  last_heartbeat_at?: string | null;
  message?: string;
  error_message?: string | null;
  terminal_decision_reason?: string | null;
  completion_marker?: Record<string, unknown> | null;
  child_pid?: number | null;
  child_process_group_id?: number | null;
  worker_id?: string | null;
  worker_handshake_at?: string | null;
  cancel_requested?: boolean;
  termination_requested_at?: string | null;
  termination_reason?: string | null;
  child_exit_code?: number | null;
  child_exit_code_protocol?: string | null;
  runner_exit_category?: string | null;
  runner_exit_report?: Record<string, unknown> | null;
  completion_marker_protocol?: string | null;
  completion_marker_validation?: Record<string, unknown> | null;
  result_integrity_status?: string | null;
  result_integrity_evidence?: Record<string, unknown> | null;
  result_counts?: Record<string, unknown>;
  stdout_bytes?: number | null;
  stdout_digest?: string | null;
  stdout_truncated?: boolean | null;
  resource_limit_evidence?: Record<string, unknown> | null;
  recovery_observed_at?: string | null;
  recovery_action?: string | null;
  recovery_process_state?: Record<string, unknown> | null;
  failure_phase?: string | null;
  failure_type?: string | null;
};

export type BacktestRunListResponse = { items: BacktestRun[] };
export type StrategyBacktestWorkspace = { strategy: Record<string, unknown>; published_revisions: Array<Record<string, unknown>>; formal_gate: Record<string, unknown>; runs: BacktestRunListResponse & { next_cursor?: string | null; has_more?: boolean } };

export type BacktestResultPage<T = Record<string, unknown>> = {
  items: T[];
  next_cursor?: string | null;
  has_more?: boolean;
  truncated?: boolean;
};

/** Structured error preserving canonical HTTP status/code for workspace UI. */
export class BacktestApiError extends Error {
  constructor(message: string, readonly status: number, readonly code?: string) {
    super(message);
    this.name = "BacktestApiError";
  }
}

/** Protocol identifier for the page's visibility-aware polling contract. */
export const FOREGROUND_POLLING_PROTOCOL = "foreground_polling@1";
export const FOREGROUND_POLL_INTERVAL_MS = 5_000;
export const TERMINAL_BACKTEST_STATUSES = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "timed_out",
  "indeterminate"
]);

export function isTerminalBacktestStatus(status: string): boolean {
  return TERMINAL_BACKTEST_STATUSES.has(status);
}

async function request(path: string, init: RequestInit = {}, signal?: AbortSignal): Promise<unknown> {
  const response = await fetch(path, {
    ...init,
    signal: signal ?? init.signal,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${readApiToken() || ""}`,
      ...init.headers
    }
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { detail?: string | { message?: string; code?: string } };
    const detail = typeof body.detail === "string" ? body.detail : body.detail?.message;
    const code = typeof body.detail === "object" ? body.detail?.code : undefined;
    throw new BacktestApiError(detail || `回测请求失败（HTTP ${response.status}）。`, response.status, code);
  }
  return response.json();
}

export const listBacktestRuns = (signal?: AbortSignal, strategyId?: string): Promise<BacktestRunListResponse> =>
  request(strategyId ? `/api/admin/backtest-runs/strategies/${encodeURIComponent(strategyId)}/backtests` : "/api/admin/backtest-runs", {}, signal) as Promise<BacktestRunListResponse>;
export const fetchStrategyBacktestWorkspace = (strategyId: string, signal?: AbortSignal): Promise<StrategyBacktestWorkspace> =>
  request(`/api/admin/strategies/${encodeURIComponent(strategyId)}/backtests`, {}, signal) as Promise<StrategyBacktestWorkspace>;
export const createBacktestRun=(payload:unknown, idempotencyKey?: string)=>{
  const body = payload && typeof payload === "object" ? {...payload as Record<string, unknown>, ...(idempotencyKey ? {idempotency_key: idempotencyKey} : {})} : payload;
  return request("/api/admin/backtest-runs",{method:"POST",headers:idempotencyKey ? {"Idempotency-Key": idempotencyKey} : undefined,body:JSON.stringify(body)});
};
export const getBacktestRun=(id:string, signal?: AbortSignal)=>request(`/api/admin/backtest-runs/${id}`, {}, signal) as Promise<BacktestRun>;
export const cancelBacktestRun=(id:string)=>request(`/api/admin/backtest-runs/${id}/cancel`,{method:"POST"});
export const compareBacktestRuns=(run_ids:string[])=>request(`/api/admin/backtest-runs/compare`,{method:"POST",body:JSON.stringify({run_ids})});

export async function fetchBacktestResult<T = Record<string, unknown>>(runId: string, kind: string, cursor?: string, signal?: AbortSignal): Promise<BacktestResultPage<T>> {
  const params = new URLSearchParams({ limit: "100" });
  if (cursor) params.set("cursor", cursor);
  return request(`/api/admin/backtest-runs/${encodeURIComponent(runId)}/results/${kind}?${params}`, {}, signal) as Promise<BacktestResultPage<T>>;
}

export const rerunBacktest = (run: BacktestRun, idempotencyKey = crypto.randomUUID()) => createBacktestRun({ strategy_revision_id: (run as BacktestRun & { strategy_revision_id?: string }).strategy_revision_id, spec: (run as BacktestRun & { spec?: unknown }).spec }, idempotencyKey);
