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
  run_kind?: "backtest_run" | "internal_link_acceptance" | string;
  preflight_profile?: string;
  preflight_profile_key?: string;
  preflight_profile_version?: number;
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
  data_revision_summary?: Record<string, unknown> | null;
  admission_report_hash?: string | null;
  session_report_hash?: string | null;
  hash_match?: boolean | null;
  report_diff?: Array<Record<string, unknown>> | null;
  failure_phase?: string | null;
  title?: string;
  message?: string;
}

/**
 * The deliberately small set of revision evidence that may be compared
 * between runs.  Keep this projection explicit: report hashes, raw source
 * revision maps, and unrelated capability details are not functional
 * revision fields and must never leak into the comparison result.
 */
export interface DataRevisionComparisonFields {
  revision_vector_hash: unknown;
  source: unknown;
  accepted_at_range: unknown;
  affected_range: unknown;
  non_strict_pit_capabilities: unknown;
  non_strict_pit: unknown;
}

function revisionCapability(item: BacktestPreflightItem): Record<string, unknown> | null {
  const summary = item.data_revision_summary;
  if (!summary || typeof summary !== "object") return null;
  const capabilities = summary.capabilities;
  if (!capabilities || typeof capabilities !== "object") return null;
  const bars = (capabilities as Record<string, unknown>).bars;
  return bars && typeof bars === "object" ? bars as Record<string, unknown> : null;
}

export function compareDataRevisionFields(left: BacktestPreflightItem, right: BacktestPreflightItem) {
  const leftBars = revisionCapability(left);
  const rightBars = revisionCapability(right);
  const leftFields: DataRevisionComparisonFields = {
    revision_vector_hash: left.data_revision_summary?.revision_vector_hash ?? leftBars?.revision_vector_hash ?? null,
    source: leftBars?.source ?? null,
    accepted_at_range: leftBars?.accepted_at_range ?? null,
    affected_range: leftBars?.affected_range ?? null,
    non_strict_pit_capabilities: left.non_strict_pit_capabilities ?? null,
    non_strict_pit: left.non_strict_pit ?? null,
  };
  const rightFields: DataRevisionComparisonFields = {
    revision_vector_hash: right.data_revision_summary?.revision_vector_hash ?? rightBars?.revision_vector_hash ?? null,
    source: rightBars?.source ?? null,
    accepted_at_range: rightBars?.accepted_at_range ?? null,
    affected_range: rightBars?.affected_range ?? null,
    non_strict_pit_capabilities: right.non_strict_pit_capabilities ?? null,
    non_strict_pit: right.non_strict_pit ?? null,
  };
  const fields = Object.keys(leftFields) as (keyof DataRevisionComparisonFields)[];
  return fields.reduce<Record<string, { left: unknown; right: unknown }>>((diff, field) => {
    const a = leftFields[field];
    const b = rightFields[field];
    if (JSON.stringify(a) !== JSON.stringify(b)) diff[field] = { left: a, right: b };
    return diff;
  }, {});
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

/** Fetch persisted formal-run comparison from the canonical compare route. */
export async function compareBacktestRuns(runIds: string[], signal?: AbortSignal): Promise<unknown> {
  const response = await fetch("/api/admin/backtests/compare", {
    method: "POST",
    headers: { ...headers(), "Content-Type": "application/json" },
    body: JSON.stringify({ run_ids: runIds }),
    signal,
  });
  await checkResponse(response);
  return response.json();
}
