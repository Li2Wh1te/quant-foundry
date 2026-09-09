import { readApiToken } from "../auth/tokenStorage";
import type { OverviewRun } from "./overview";

export interface SourceField {
  key: string; label: string; type: "url" | "secret" | "string" | "integer" | "number" | "boolean";
  required: boolean; default?: string | number | boolean; help?: string;
}
export interface SourceCapability {
  key: string; name: string; english_name: string; task_count: number; available: boolean;
  last_run: Pick<OverviewRun, "id" | "task_id" | "status" | "created_at" | "started_at" | "finished_at"> | null;
}
export interface DataSource {
  key: string; name: string; fields: SourceField[]; values: Record<string, string | number | boolean>;
  secret_fields_configured: string[]; configured: boolean; enabled: boolean; version: number;
  checked_at: string | null; check_status: string; check_message: string | null;
  capabilities: SourceCapability[]; skipped_runs?: number;
}
export interface SourceProbe {
  ok: boolean; status: string; message: string; checked_at: string; version: number; saved: false;
}
export type SourceDraft = Record<string, string | number | boolean>;

export class DataSourceApiError extends Error {
  constructor(readonly status: number, message: string, readonly field?: string) {
    super(message); this.name = "DataSourceApiError";
  }
}

async function request<T>(path: string, method = "GET", body?: unknown, signal?: AbortSignal): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal?.addEventListener("abort", abort, { once: true });
  if (signal?.aborted) abort();
  const timer = setTimeout(abort, 35000);
  try {
    const token = readApiToken();
    const response = await fetch(`/api/data-sources${path}`, {
      method, headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
      body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store", signal: controller.signal
    });
    if (!response.ok) {
      // Only this API's sanitized Chinese error envelope is operator copy.
      // Do not render proxy HTML, validation input, or unexpected server traces.
      const payload = await response.json().catch(() => null);
      const detail = payload?.detail;
      const message = typeof detail?.message === "string" && detail.message.length <= 500 && /[\u4e00-\u9fff]/.test(detail.message)
        ? detail.message : `请求未完成（HTTP ${response.status}），请刷新后重试。`;
      throw new DataSourceApiError(response.status, message, typeof detail?.field === "string" ? detail.field : undefined);
    }
    return await response.json() as T;
  } catch (error) {
    if (signal?.aborted) throw new DOMException("Request aborted", "AbortError");
    if (error instanceof DataSourceApiError) throw error;
    throw new DataSourceApiError(0, method === "GET" ? "数据源加载失败，请检查网络后重试。"
      : "未能确认请求结果，请重新加载配置后核对；不要重复提交。" );
  } finally {
    clearTimeout(timer); signal?.removeEventListener("abort", abort);
  }
}

export const listDataSources = (signal?: AbortSignal) => request<{ items: DataSource[] }>("", "GET", undefined, signal);
export const readDataSource = (key: string) => request<DataSource>(`/${encodeURIComponent(key)}`);
export const testDataSource = (source: DataSource, fields: SourceDraft) => request<SourceProbe>(`/${encodeURIComponent(source.key)}/test`, "POST", { version: source.version, fields });
export const saveDataSource = (source: DataSource, fields: SourceDraft) => request<DataSource>(`/${encodeURIComponent(source.key)}/config`, "PUT", { version: source.version, fields });
export const setDataSourceEnabled = (source: DataSource, enabled: boolean) => request<DataSource>(`/${encodeURIComponent(source.key)}/state`, "PUT", { version: source.version, enabled });

/** Provider schema drives the form. Secret values are never initialized from
 * public metadata, even if an unexpected field appears in the response. */
export function sourceDraft(source: DataSource): SourceDraft {
  return Object.fromEntries(source.fields.map(field => [field.key, field.type === "secret" ? "" : source.values[field.key] ?? field.default ?? ""]));
}
export function validateSourceDraft(source: DataSource, draft: SourceDraft): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const field of source.fields) {
    const value = draft[field.key];
    const blank = value === undefined || value === "" || typeof value === "string" && !value.trim();
    if (field.required && blank && !(field.type === "secret" && source.secret_fields_configured.includes(field.key))) {
      errors[field.key] = `请填写${field.label}。`; continue;
    }
    if (!blank && field.type === "url") {
      try {
        const url = new URL(String(value));
        if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error();
      } catch { errors[field.key] = `${field.label}须为 HTTP(S) 地址，且不能包含凭据、查询参数或片段。`; }
    }
    if (!blank && (field.type === "integer" || field.type === "number") &&
      (!Number.isFinite(Number(value)) || field.type === "integer" && !Number.isInteger(Number(value)))) errors[field.key] = `请填写有效的${field.label}。`;
  }
  return errors;
}
export function sourceState(source: DataSource): { label: string; tone: "neutral" | "ok" | "warn" } {
  if (!source.enabled) return { label: "已停用", tone: "neutral" };
  if (!source.configured) return { label: "待配置", tone: "warn" };
  if (source.check_status === "connected") return { label: "连接正常", tone: "ok" };
  if (source.check_status === "not_checked") return { label: "未检测", tone: "neutral" };
  return { label: "检查异常", tone: "warn" };
}
