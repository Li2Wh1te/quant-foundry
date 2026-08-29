import { readApiToken } from "../auth/tokenStorage";

export type PreflightSection = "calendar" | "sessions";

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
  phase: string;
  status: string;
  report_hash: string;
  hash_schema_version: number;
  section?: PreflightSection | null;
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
}

export interface BacktestPreflightPage {
  items: BacktestPreflightItem[];
  next_cursor: string | null;
  has_more: boolean;
  truncated: boolean;
}

export class BacktestPreflightError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "BacktestPreflightError";
    this.status = status;
  }
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

/**
 * Fetch the canonical persisted preflight resource.  The old `/backtests`
 * alias is intentionally not used: it is only a server-side migration
 * redirect and is sunset in API version 4.
 */
export async function fetchBacktestPreflight(
  runId: string,
  options: { section?: PreflightSection; limit?: number; cursor?: string } = {},
  signal?: AbortSignal
): Promise<BacktestPreflightPage> {
  const params = new URLSearchParams();
  if (options.section) params.set("section", options.section);
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  if (options.cursor) params.set("cursor", options.cursor);
  const query = params.toString();
  const url = `/api/admin/backtest-runs/${encodeURIComponent(runId)}/results/data-preflight${query ? `?${query}` : ""}`;
  const response = await fetch(url, { headers: headers(), signal });
  await checkResponse(response);
  return response.json() as Promise<BacktestPreflightPage>;
}
