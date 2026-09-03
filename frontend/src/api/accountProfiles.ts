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

export function listAccountProfiles(
  name?: string,
  status: AccountProfileStatus | "" = "active",
): Promise<AccountProfile[]> {
  const params = new URLSearchParams({ limit: "500" });
  if (name?.trim()) params.set("name", name.trim());
  if (status) params.set("status", status);
  return request<AccountProfile[]>(`/api/admin/backtest-account-profiles?${params}`);
}

export function createAccountProfile(payload: AccountProfilePayload): Promise<AccountProfile> {
  return request<AccountProfile>("/api/admin/backtest-account-profiles", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateAccountProfile(
  id: string,
  payload: Partial<AccountProfilePayload>,
): Promise<AccountProfile> {
  return request<AccountProfile>(`/api/admin/backtest-account-profiles/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function deleteAccountProfile(id: string): Promise<void> {
  return request<void>(`/api/admin/backtest-account-profiles/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}
