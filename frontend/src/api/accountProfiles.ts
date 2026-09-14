import { readApiToken } from "../auth/tokenStorage";

export type AccountProfileStatus = "active" | "inactive" | "retired";

export interface FeeRule {
  key: string;
  category: string;
  side: string | null;
  rate: string;
  minimum: string;
  fixed_amount: string;
  rounding_level: "fee_item" | "fill" | "order" | null;
  rounding_scope: string | null;
  rounding_mode: "up" | "down" | "half_up" | null;
  rounding_precision: string | null;
  applicability: Record<string, string>;
}

export interface FeeSchedule {
  key: string;
  version?: number;
  fee_rules: FeeRule[];
  metadata: Record<string, string>;
}

export interface AccountProfile {
  id: string;
  name: string;
  status: AccountProfileStatus;
  version: number;
  fee_schedule_version: number;
  fee_schedule: FeeSchedule;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface AccountProfilePayload {
  name: string;
  status: AccountProfileStatus;
  fee_schedule: FeeSchedule;
  metadata: Record<string, string>;
}

export class AccountProfileApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "AccountProfileApiError";
  }
}

function headers(): HeadersInit {
  const token = readApiToken();
  return token
    ? { Authorization: `Bearer ${token}`, "Content-Type": "application/json" }
    : { "Content-Type": "application/json" };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, headers: { ...headers(), ...init?.headers } });
  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）。`;
    try {
      const body = await response.json() as { detail?: unknown };
      if (typeof body.detail === "string") message = body.detail;
    } catch {
      // Keep the HTTP fallback when the server returned no JSON body.
    }
    throw new AccountProfileApiError(message, response.status);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function createAccountProfile(payload: AccountProfilePayload): Promise<AccountProfile> {
  return request<AccountProfile>("/api/admin/backtest-account-profiles", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateAccountProfile(
  id: string,
  payload: Partial<AccountProfilePayload> & { expected_version?: number },
): Promise<AccountProfile> {
  return request<AccountProfile>(`/api/admin/backtest-account-profiles/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

/** Resolve historical account configuration without silently using latest. */
export function listAccountProfileVersions(id: string): Promise<AccountProfile[]> {
  return request(`/api/admin/backtest-account-profiles/${encodeURIComponent(id)}/versions`);
}


export interface AccountOverview {
  total_accounts: number;
  active_accounts: number;
  inactive_accounts: number;
  retired_accounts: number;
  total_fee_rules: number;
  related_strategy_versions: number;
  used_accounts: number;
}
export interface AccountPage { items: AccountProfile[]; total: number; limit: number; offset: number }
export interface AccountUsageItem {
  account_profile_version: string | null;
  strategy_revision_id: string | null;
  strategy_id: string | null;
  strategy_name: string | null;
  revision_number: number | null;
  run_count: number;
  latest_run_id: string;
  latest_run_at: string;
  latest_run_status: string;
}
export interface AccountUsage {
  items: AccountUsageItem[];
  total: number;
  total_runs: number;
  related_strategy_versions: number;
  limit: number;
  offset: number;
}
const accountBase = "/api/admin/backtest-account-profiles";
export function getAccountOverview(signal?: AbortSignal): Promise<AccountOverview> {
  return request(`${accountBase}/overview`, { signal });
}
export function getAccountPage(keyword: string, status: AccountProfileStatus | "", offset = 0, signal?: AbortSignal): Promise<AccountPage> {
  const params = new URLSearchParams({ limit: "20", offset: String(offset) });
  if (keyword.trim()) params.set("keyword", keyword.trim());
  if (status) params.set("status", status);
  return request(`${accountBase}/page?${params}`, { signal });
}
export function getAccountUsage(id: string, offset = 0, signal?: AbortSignal): Promise<AccountUsage> {
  return request(`${accountBase}/${encodeURIComponent(id)}/usage?limit=5&offset=${offset}`, { signal });
}
export function getAccountProfile(id: string): Promise<AccountProfile> {
  return request(`${accountBase}/${encodeURIComponent(id)}`);
}

export interface AccountDeletionCheck { can_permanently_delete: boolean; reason: string | null }
export function checkAccountDeletion(id: string, signal?: AbortSignal): Promise<AccountDeletionCheck> {
  return request(`${accountBase}/${encodeURIComponent(id)}/deletion-check`, { signal });
}
export function permanentlyDeleteAccount(id: string, expectedVersion: number): Promise<void> {
  return request(`${accountBase}/${encodeURIComponent(id)}/permanent?expected_version=${expectedVersion}`, { method: "DELETE" });
}
