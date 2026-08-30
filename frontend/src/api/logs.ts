import { readApiToken } from "../auth/tokenStorage";

export type LogLevel = "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";
export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE" | "OPTIONS" | "HEAD";
export type StatusClass = "2xx" | "3xx" | "4xx" | "5xx";

export interface LogEntry {
  timestamp?: string;
  level?: string;
  event?: string;
  method?: string;
  path?: string;
  status_code?: number;
  duration_ms?: number;
  request_id?: string;
  service?: string;
  environment?: string;
  /** Common scheduler context fields retained for the expanded detail view. */
  message?: string;
  task_id?: string;
  run_id?: string;
  task_type?: string;
  error_type?: string;
  error_message?: string;
  stack?: string;
  exception?: string;
  [key: string]: unknown;
}

export interface LogFacets {
  levels: Record<string, number>;
  methods: Record<string, number>;
  status_classes: Record<string, number>;
  paths: Record<string, number>;
}

export interface LogSearchResult {
  items: LogEntry[];
  matched_count: number;
  truncated: boolean;
  scanned_files: number;
  facets: LogFacets;
}

export interface LogSearchFilters {
  keyword?: string;
  level?: LogLevel;
  method?: HttpMethod;
  statusClass?: StatusClass;
  path?: string;
  startTime?: string;
  endTime?: string;
  limit?: number;
}

export class UnauthorizedError extends Error {
  constructor() {
    super("登录状态已失效，请重新登录。");
    this.name = "UnauthorizedError";
  }
}

function authorizedHeaders(): HeadersInit {
  const token = readApiToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function ensureSuccessful(response: Response): Promise<void> {
  if (response.status === 401) {
    throw new UnauthorizedError();
  }
  if (!response.ok) {
    let detail = `请求失败（HTTP ${response.status}）。`;
    try {
      const body = await response.json() as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // Keep the HTTP status fallback when the response is not JSON.
    }
    throw new Error(detail);
  }
}

export async function searchLogs(
  filters: LogSearchFilters,
  signal?: AbortSignal
): Promise<LogSearchResult> {
  const params = new URLSearchParams();
  if (filters.keyword) params.set("keyword", filters.keyword);
  if (filters.level) params.set("level", filters.level);
  if (filters.method) params.set("method", filters.method);
  if (filters.statusClass) params.set("status_class", filters.statusClass);
  if (filters.path) params.set("path", filters.path);
  if (filters.startTime) params.set("start_time", filters.startTime);
  if (filters.endTime) params.set("end_time", filters.endTime);
  params.set("limit", String(filters.limit ?? 200));

  const response = await fetch(`/api/admin/logs?${params}`, {
    headers: authorizedHeaders(),
    signal
  });
  await ensureSuccessful(response);
  return response.json() as Promise<LogSearchResult>;
}

export async function clearLogs(): Promise<void> {
  const response = await fetch("/api/admin/logs/clear", {
    method: "POST",
    headers: authorizedHeaders()
  });
  await ensureSuccessful(response);
}
