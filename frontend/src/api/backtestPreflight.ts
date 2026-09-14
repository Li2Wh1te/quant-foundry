import { readApiToken } from "../auth/tokenStorage";

export interface PreflightIssue {
  code: string;
  title: string;
  message: string;
  scope?: string | null;
  field?: string | null;
  date?: string | null;
  date_range?: string[] | null;
  calendar_id?: string | null;
  values_by_calendar?: Record<string, unknown> | null;
  [key: string]: unknown;
}

export interface BacktestPreflightItem {
  run_id: string;
  run_kind?: "backtest_run" | "internal_link_acceptance" | string;
  preflight_profile?: string;
  preflight_profile_key?: string;
  preflight_profile_version?: number;
  phase: string;
  status: string;
  report_hash: string;
  hash_schema_version: number;
  section?: "calendar" | "sessions" | null;
  capabilities?: Record<string, unknown> | null;
  calendar_summary?: Record<string, unknown> | null;
  session_summary?: Record<string, unknown> | null;
  pit_status?: string | null;
  data_cutoff?: string | null;
  cutoff_local_date?: string | null;
  include_cutoff_day?: boolean | null;
  knowledge_as_of?: string | null;
  pit_profile?: string | null;
  profile_version?: string | null;
  non_strict_pit?: boolean | null;
  non_strict_pit_capabilities?: string[] | null;
  calendar_revision_digest?: string | null;
  snapshot_fingerprint?: string | null;
  coverage?: Record<string, unknown> | null;
  source_revisions?: Record<string, unknown> | null;
  data_revision_summary?: Record<string, unknown> | null;
  admission_report_hash?: string | null;
  session_report_hash?: string | null;
  hash_match?: boolean | null;
  report_diff?: Array<Record<string, unknown>> | null;
  failure_phase?: string | null;
  title?: string;
  message?: string;
}

export class BacktestPreflightError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "BacktestPreflightError";
    this.status = status;
  }
}

/** Execute side-effect-free admission preflight before creating a formal run. */
export async function preflightBacktest(payload: unknown, signal?: AbortSignal): Promise<BacktestPreflightItem> {
  const response = await fetch("/api/admin/backtest-runs/preflight", {
    method: "POST", headers: { ...headers(), "Content-Type": "application/json" },
    body: JSON.stringify(payload), signal,
  });
  await checkResponse(response);
  return response.json() as Promise<BacktestPreflightItem>;
}

function headers(): HeadersInit {
  const token = readApiToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function checkResponse(response: Response): Promise<void> {
  if (response.status === 401) {
    throw new BacktestPreflightError("登录状态已失效，请重新登录。", 401);
  }
  if (response.ok) return;
  let message = `预检详情加载失败（HTTP ${response.status}）。`;
  try {
    const body = await response.json() as { detail?: unknown };
    if (typeof body.detail === "string") message = body.detail;
  } catch {
    // Preserve the stable HTTP fallback for non-JSON responses.
  }
  throw new BacktestPreflightError(message, response.status);
}
