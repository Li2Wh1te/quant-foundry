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
  strategy_revision_id?: string | null;
  parameters?: Record<string, unknown>;
  backtest_config?: Record<string, unknown>;
  data_request?: Record<string, unknown>;
  behavior_versions?: Record<string, unknown>;
  formal_gates?: Record<string, unknown>;
  account_profile_id?: string | null;
  account_profile_version?: string | null;
  fee_schedule_key?: string | null;
  fee_schedule_version?: string | null;
  random_seed?: number | null;
  completion_marker?: Record<string, unknown> | null;
  child_pid?: number | null;
  child_process_group_id?: number | null;
  worker_id?: string | null;
  worker_handshake_at?: string | null;
  cancel_requested?: boolean;
  termination_requested_at?: string | null;
  termination_reason?: string | null;
  forced_termination?: boolean;
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
  stdout_evidence?: Record<string, unknown> | null;
  resource_limit_evidence?: Record<string, unknown> | null;
  recovery_observed_at?: string | null;
  recovery_action?: string | null;
  recovery_process_state?: Record<string, unknown> | null;
  failure_phase?: string | null;
  failure_step?: number | null;
  failure_type?: string | null;
  source_line?: number | null;
  technical_detail?: string | null;
  failure_evidence?: Record<string, unknown> | null;
};

export interface BacktestInitialPositionInput {
  instrument_id: string;
  side: "long" | "short" | "net";
  quantity: string | number;
  available_quantity: string | number;
  average_price?: string | number | null;
}

export interface BacktestConfigInput {
  start_date: string;
  end_date: string;
  initial_cash: string | number;
  initial_positions: BacktestInitialPositionInput[];
  dynamic_universe: boolean;
  instrument_ids: string[];
  exchanges: string[];
  strategy_price_bases: Array<"raw" | "qfq" | "hfq">;
  currency: string;
  timezone: string;
  frequency: "1d";
  warmup_sessions: number;
}

export interface ComponentSelectionInput {
  key: string;
  version: number;
  parameters: Record<string, unknown>;
}

export interface BacktestRunCreateInput {
  strategy_revision_id: string;
  parameters?: Record<string, unknown> | null;
  backtest_config: BacktestConfigInput;
  account_profile_id?: string | null;
  slippage_model: ComponentSelectionInput;
  random_seed?: number | null;
  idempotency_key?: string;
  degraded?: boolean;
  confirmed_admission_report_hash?: string | null;
}

export interface ComponentDescriptor {
  component_kind: string;
  key: string;
  version: number;
  name_zh: string;
  name_en: string;
  display_name: string;
  parameter_schema: Record<string, unknown>;
  capabilities: Record<string, unknown>;
}

export type BacktestRunListResponse = { items: BacktestRun[] };
export type StrategyBacktestWorkspace = {
  strategy: Record<string, unknown>;
  published_revisions: Array<Record<string, unknown>>;
  slippage_models: ComponentDescriptor[];
  formal_gate: Record<string, unknown>;
  runs: BacktestRunListResponse & { next_cursor?: string | null; has_more?: boolean };
};

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
export const createBacktestRun=(payload:BacktestRunCreateInput, idempotencyKey?: string)=>{
  const body = {...payload, ...(idempotencyKey ? {idempotency_key: idempotencyKey} : {})};
  return request("/api/admin/backtests",{method:"POST",headers:idempotencyKey ? {"Idempotency-Key": idempotencyKey} : undefined,body:JSON.stringify(body)});
};
export const getBacktestRun=(id:string, signal?: AbortSignal)=>request(`/api/admin/backtest-runs/${id}`, {}, signal) as Promise<BacktestRun>;
export const cancelBacktestRun=(id:string)=>request(`/api/admin/backtest-runs/${id}/cancel`,{method:"POST"});
export const compareBacktestRuns=(run_ids:string[])=>request(`/api/admin/backtest-runs/compare`,{method:"POST",body:JSON.stringify({run_ids})});

export async function fetchBacktestResult<T = Record<string, unknown>>(runId: string, kind: string, cursor?: string, signal?: AbortSignal): Promise<BacktestResultPage<T>> {
  const params = new URLSearchParams({ limit: "100" });
  if (cursor) params.set("cursor", cursor);
  return request(`/api/admin/backtest-runs/${encodeURIComponent(runId)}/results/${kind}?${params}`, {}, signal) as Promise<BacktestResultPage<T>>;
}

export const rerunBacktest = (run: BacktestRun, idempotencyKey = crypto.randomUUID()) =>
  request(`/api/admin/backtests/${encodeURIComponent(run.run_id)}/rerun`, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey }
  }) as Promise<BacktestRun>;
