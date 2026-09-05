import { readApiToken } from "../auth/tokenStorage";

export type StrategyState = "active" | "archived";

export interface StrategySummary {
  id: string;
  name: string;
  description: string | null;
  state: StrategyState;
  current_revision_id: string | null;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface StrategyDraft {
  source_code: string;
  source_hash: string;
  parameter_schema: Record<string, unknown>;
  default_parameters: Record<string, unknown>;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface StrategyRevisionSummary {
  id: string;
  revision_number: number;
  source_hash: string;
  runtime_manifest: Record<string, unknown>;
  published_at: string;
}

export interface StrategyRevision extends StrategyRevisionSummary {
  source_code: string;
  parameter_schema: Record<string, unknown>;
  default_parameters: Record<string, unknown>;
}

export interface StrategyDetail extends StrategySummary {
  draft_changed_since_revision?: boolean;
  draft: StrategyDraft;
  current_revision: StrategyRevisionSummary | null;
}

export interface StrategyValidationIssue {
  code: string;
  message: string;
  line: number | null;
  column: number | null;
}

export interface StrategyValidationResult {
  valid: boolean;
  draft_version: number;
  source_hash: string;
  issues: StrategyValidationIssue[];
}

export interface StrategyCreatePayload {
  name: string;
  description?: string;
  source_code: string;
  parameter_schema: Record<string, unknown>;
  default_parameters: Record<string, unknown>;
}

export interface StrategyDraftPayload {
  version: number;
  source_code?: string;
  parameter_schema?: Record<string, unknown>;
  default_parameters?: Record<string, unknown>;
}

export interface StrategyMetadataPayload {
  version: number;
  name?: string;
  description?: string | null;
}

export class StrategyApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly issues: StrategyValidationIssue[] = []
  ) {
    super(message);
    this.name = "StrategyApiError";
  }
}

function headers(): HeadersInit {
  const token = readApiToken();
  return token
    ? {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json"
      }
    : { "Content-Type": "application/json" };
}

function isValidationIssue(value: unknown): value is StrategyValidationIssue {
  if (!value || typeof value !== "object") return false;
  const issue = value as Record<string, unknown>;
  return typeof issue.code === "string" && typeof issue.message === "string";
}

function parseErrorDetail(detail: unknown): { message: string; issues: StrategyValidationIssue[] } {
  if (typeof detail === "string") return { message: detail, issues: [] };
  if (!detail || typeof detail !== "object") return { message: "策略请求失败。", issues: [] };

  const body = detail as Record<string, unknown>;
  const issues = Array.isArray(body.issues) ? body.issues.filter(isValidationIssue) : [];
  if (typeof body.message === "string") return { message: body.message, issues };
  return { message: "策略请求校验失败。", issues };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { ...headers(), ...init?.headers }
  });

  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）。`;
    let issues: StrategyValidationIssue[] = [];
    try {
      const body = await response.json() as { detail?: unknown };
      ({ message, issues } = parseErrorDetail(body.detail));
    } catch {
      // Keep the HTTP fallback when the response is not JSON.
    }
    throw new StrategyApiError(message, response.status, issues);
  }

  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function listStrategies(includeArchived = false): Promise<StrategySummary[]> {
  const params = new URLSearchParams({ include_archived: String(includeArchived) });
  return request<StrategySummary[]>(`/api/admin/strategies?${params}`);
}

export function getStrategy(strategyId: string): Promise<StrategyDetail> {
  return request<StrategyDetail>(`/api/admin/strategies/${encodeURIComponent(strategyId)}`);
}

export function createStrategy(payload: StrategyCreatePayload): Promise<StrategyDetail> {
  return request<StrategyDetail>("/api/admin/strategies", {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function updateStrategyMetadata(
  strategyId: string,
  payload: StrategyMetadataPayload
): Promise<StrategySummary> {
  return request<StrategySummary>(`/api/admin/strategies/${encodeURIComponent(strategyId)}`, {
    method: "PATCH",
    body: JSON.stringify(payload)
  });
}

export function saveStrategyDraft(
  strategyId: string,
  payload: StrategyDraftPayload
): Promise<StrategyDraft> {
  return request<StrategyDraft>(`/api/admin/strategies/${encodeURIComponent(strategyId)}/draft`, {
    method: "PATCH",
    body: JSON.stringify(payload)
  });
}

export function validateStrategy(strategyId: string): Promise<StrategyValidationResult> {
  return request<StrategyValidationResult>(
    `/api/admin/strategies/${encodeURIComponent(strategyId)}/validate`,
    { method: "POST" }
  );
}

export function publishStrategy(
  strategyId: string,
  draftVersion: number
): Promise<StrategyRevision> {
  return request<StrategyRevision>(`/api/admin/strategies/${encodeURIComponent(strategyId)}/publish`, {
    method: "POST",
    body: JSON.stringify({ draft_version: draftVersion })
  });
}

export function listStrategyRevisions(strategyId: string): Promise<StrategyRevisionSummary[]> {
  return request<StrategyRevisionSummary[]>(
    `/api/admin/strategies/${encodeURIComponent(strategyId)}/revisions`
  );
}

export function getStrategyRevision(
  strategyId: string,
  revisionNumber: number
): Promise<StrategyRevision> {
  return request<StrategyRevision>(
    `/api/admin/strategies/${encodeURIComponent(strategyId)}/revisions/${revisionNumber}`
  );
}

export function archiveStrategy(strategy: StrategySummary): Promise<void> {
  return request<void>(
    `/api/admin/strategies/${encodeURIComponent(strategy.id)}?version=${strategy.version}`,
    { method: "DELETE" }
  );
}
