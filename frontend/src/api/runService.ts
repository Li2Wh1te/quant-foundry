import { readApiToken } from "../auth/tokenStorage";

export type Frequency = "1d" | "1m" | "5m" | "15m" | "30m" | "60m" | "tick";
export type ExecutionModel = "bar_next_interval_v1" | "trade_tick_v1" | "quote_tick_v1";
export interface RunConfig {
  api_schema: "qf.backtest.v2"; strategy_revision_id: string; account_id: string;
  initial_cash: string; start: string; end: string; frequency: Frequency;
  universe: string[]; parameters: Record<string, unknown>; execution_model: ExecutionModel;
  currency?: "CNY"; reference_calendar?: string; benchmark?: string | null; seed?: number;
  participation_rate?: string; slippage_bps?: string; risk_free_rate?: string;
  annualization_sessions?: number; result_sampling?: "session_close" | "bar_close";
  cost_overrides?: { commission_rate?: string; minimum_commission?: string };
}
export interface RunIssue { code: string; operation?: string; message?: string }
export interface RunSummary {
  run_id: string; engine_kind: "rust" | "legacy_python"; api_schema: string; status: string;
  cancel_requested?: boolean; partial?: boolean; error?: RunIssue | null;
  progress_completed?: number; progress_total?: number | null; normalized_config?: RunConfig;
  created_at?: string; finished_at?: string | null; batch_id?: string | null; retry_of?: string | null;
  account_version?: number; cost_configuration?: Record<string, unknown>;
}
export interface RunPage { items: RunSummary[]; has_more: boolean; next_cursor: string | null }
export interface BatchPage extends RunPage { batch_id: string }
export interface Capabilities {
  api_schema: "qf.backtest.v2";
  engine: { installed: boolean; frequencies: Frequency[]; execution_models: ExecutionModel[]; result_kinds: string[] };
  actual_data: { binding: string; frequency?: Frequency; available: boolean; synthetic?: boolean; reason?: string; limitations?: string[] }[];
  limits: { batch_runs: number; parameter_bytes: number; parameter_depth: number; compare_runs: number };
}
export interface Preflight { errors: RunIssue[]; warnings: string[]; normalized_config: RunConfig | null }
export interface StrategyChoice { id: string; name: string; state: string }
export interface RevisionChoice { id: string; revision_number: number; alias?: string; parameter_schema: Record<string, unknown>; default_parameters: Record<string, unknown> }
export interface AccountChoice { id: string; name: string; status: string; version: number; fee_schedule: Record<string, unknown> }

const messages: Record<string, string> = {
  CAPABILITY_UNAVAILABLE: "所需引擎、数据频率或隔离能力尚未就绪。请确认能力后重新预检。",
  DATA_RESTRICTED: "数据、策略、账户或运行不可读取。请核对权限与 R01 数据门禁。",
  DATA_CHANGED: "运行期间数据或读取条件发生变化，本次已终止。请重新预检并运行。",
  RULE_UNAVAILABLE: "所选日期的交易规则、费用事实或参考日历尚未确认。",
  INVALID_RUN_CONFIG: "配置的格式、日期或数值不符合运行契约，请修改后重新预检。",
  IDEMPOTENCY_CONFLICT: "这次提交标识已绑定其他配置。请先确认原提交结果。",
  STRATEGY_ERROR: "策略回调或参数不符合 S3 契约，或策略执行失败。请核对源码及参数。",
  RESOURCE_LIMIT: "运行或请求超过资源预算。请缩小范围后重新预检。",
  RESULT_BUDGET_EXCEEDED: "必要结果超过存储预算，本次运行已失败。",
  RESULT_NOT_READY: "结果尚未就绪，请等待运行终结。",
  LOOKAHEAD_FORBIDDEN: "策略请求了模拟时点之后的数据，请修改查询边界。",
  NUMERIC_RANGE_UNSUPPORTED: "输入数值超出支持范围，请核对金额、价格及数量。",
  INSUFFICIENT_CASH: "订单被拒绝：可用资金不足。受理订单也不等于成交。",
  INSUFFICIENT_SELLABLE: "订单被拒绝：可卖数量不足。",
  INVALID_ORDER: "订单被拒绝：申报数量、价格或交易规则不满足要求。",
  CANCELLED: "运行已取消，已保存内容属于部分结果。",
  LEGACY_ENGINE_RETIRED: "旧引擎执行入口已退役。历史结果只读；迁入后发布新的 S3 修订。",
};
export function issueText(issue: RunIssue): string { return messages[issue.code] || "服务未能完成此操作，请核对配置与服务状态后重试。"; }
export class RunApiError extends Error {
  constructor(readonly status: number, readonly code: string) {
    super(status === 401 ? "登录状态已失效，请重新登录。" : status === 403 ? "没有回测操作权限，请联系管理员后重试。" : issueText({ code }));
    this.name = "RunApiError";
  }
}
const denials = new Set<(status: number) => void>();
const requests = new Set<AbortController>();
export function onRunAccessDenied(callback: (status: number) => void) { denials.add(callback); return () => { denials.delete(callback); }; }
async function request<T>(path: string, init: RequestInit = {}, signal?: AbortSignal): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal?.aborted) abort();
  signal?.addEventListener("abort", abort, { once: true }); requests.add(controller);
  try {
    const response = await fetch(path, { cache: "no-store", ...init, signal: controller.signal,
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${readApiToken() || ""}`, ...init.headers } });
    // Revoke cached content before reading an optional error body, which may stall.
    if (response.status === 401 || response.status === 403) {
      for (const active of requests) if (active !== controller) active.abort();
      for (const callback of denials) callback(response.status);
      void response.body?.cancel().catch(() => {});
      throw new RunApiError(response.status, "DATA_RESTRICTED");
    }
    const body = await response.json().catch(() => null);
    if (controller.signal.aborted) throw new DOMException("请求已取消", "AbortError");
    if (!response.ok) throw new RunApiError(response.status, typeof body?.detail?.code === "string" ? body.detail.code : "REQUEST_FAILED");
    if (!body || typeof body !== "object") throw new RunApiError(502, "INVALID_CONTRACT");
    return body as T;
  } finally { requests.delete(controller); signal?.removeEventListener("abort", abort); }
}
function page(value: RunPage): RunPage {
  if (!Array.isArray(value.items) || typeof value.has_more !== "boolean" ||
    value.has_more && (typeof value.next_cursor !== "string" || !value.next_cursor)) throw new RunApiError(502, "INVALID_CONTRACT");
  value.items.forEach(run);
  return value;
}
function run(value: RunSummary): RunSummary {
  if (typeof value.run_id !== "string" || !value.run_id || !["rust", "legacy_python"].includes(value.engine_kind) || typeof value.status !== "string" ||
    value.engine_kind === "rust" && (!value.normalized_config || typeof value.normalized_config.initial_cash !== "string" || value.api_schema !== "qf.backtest.v2")) throw new RunApiError(502, "INVALID_CONTRACT");
  return value;
}
const base = "/api/admin/backtests";
export async function capabilities(signal?: AbortSignal) {
  const value = await request<Capabilities>(`${base}/capabilities`, {}, signal);
  if (value.api_schema !== "qf.backtest.v2" || typeof value.engine?.installed !== "boolean" ||
    !Array.isArray(value.engine.frequencies) || !Array.isArray(value.engine.execution_models) || !Array.isArray(value.actual_data) ||
    !Number.isInteger(value.limits?.batch_runs) || value.limits.batch_runs < 1 || value.limits.batch_runs > 256) throw new RunApiError(502, "INVALID_CONTRACT");
  return value;
}
export async function preflight(config: RunConfig, signal?: AbortSignal) {
  const value = await request<Preflight>(`${base}/preflight`, { method: "POST", body: JSON.stringify(config) }, signal);
  if (!Array.isArray(value.errors) || !Array.isArray(value.warnings) || value.errors.some(i => typeof i.code !== "string") ||
    !value.errors.length && (!value.normalized_config || typeof value.normalized_config.initial_cash !== "string")) throw new RunApiError(502, "INVALID_CONTRACT");
  return value;
}
export async function listRuns(cursor?: string, signal?: AbortSignal, strategyId?: string) {
  const query = new URLSearchParams({ limit: "20" }); if (cursor) query.set("cursor", cursor);
  if (strategyId) return page((await request<{ runs: RunPage }>(`/api/admin/strategies/${encodeURIComponent(strategyId)}/backtests?${query}`, {}, signal)).runs);
  return page(await request<RunPage>(`${base}?${query}`, {}, signal));
}
export async function getRun(id: string, signal?: AbortSignal) { return run(await request<RunSummary>(`${base}/${encodeURIComponent(id)}`, {}, signal)); }
export async function cancelRun(id: string, signal?: AbortSignal) { return run(await request<RunSummary>(`${base}/${encodeURIComponent(id)}/cancel`, { method: "POST" }, signal)); }
export async function getBatch(id: string, cursor?: string, signal?: AbortSignal) {
  const query = new URLSearchParams({ limit: "20" }); if (cursor) query.set("cursor", cursor);
  const value = await request<BatchPage>(`${base}/batches/${encodeURIComponent(id)}?${query}`, {}, signal); page(value); return value;
}
export async function cancelBatch(id: string, signal?: AbortSignal) { const value = await request<BatchPage>(`${base}/batches/${encodeURIComponent(id)}/cancel`, { method: "POST" }, signal); page(value); return value; }
export function resultSummary(id: string, signal?: AbortSignal) { return request<{ engine_kind: string; partial: boolean; summary: Record<string, unknown> }>(`${base}/${encodeURIComponent(id)}/results/summary?limit=1`, {}, signal); }
export function strategyPage(offset: number, signal?: AbortSignal) { return request<StrategyChoice[]>(`/api/admin/strategies?include_archived=false&limit=100&offset=${offset}`, {}, signal); }
export function revisions(id: string, signal?: AbortSignal) { return request<{ published_revisions: RevisionChoice[] }>(`/api/admin/strategies/${encodeURIComponent(id)}/backtests?limit=1`, {}, signal).then(v => v.published_revisions); }
export function accountPage(offset: number, signal?: AbortSignal) { return request<{ items: AccountChoice[]; total: number }>(`/api/admin/backtest-account-profiles/page?status=active&limit=20&offset=${offset}`, {}, signal); }

export interface Submission { key: string; path: string; body: unknown; owner: string }
const pending = new Map<string, Submission>();
const inFlight = new Map<string, Promise<unknown>>();
export function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") return `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${canonical((value as Record<string, unknown>)[k])}`).join(",")}}`;
  return JSON.stringify(value) ?? "null";
}
async function ownerKey(): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(readApiToken() || ""));
  return [...new Uint8Array(digest)].map(v => v.toString(16).padStart(2, "0")).join("");
}
export async function pendingSubmission(): Promise<Submission | null> {
  const owner = await ownerKey();
  let stored: Submission | null = pending.get(owner) || null;
  try { stored ||= JSON.parse(sessionStorage.getItem(`qf.s3.pending.${owner}`) || "null"); } catch { /* The mounted session still retains its key. */ }
  if (!stored || stored.owner !== owner || ![base, `${base}/batches`].includes(stored.path) || typeof stored.key !== "string") return null;
  pending.set(owner, stored); return stored;
}
export async function submit(body: RunConfig | { configs: RunConfig[] }): Promise<RunSummary | BatchPage> {
  const owner = await ownerKey(), path = "configs" in body ? `${base}/batches` : base;
  const existing = await pendingSubmission();
  if (existing && (existing.path !== path || canonical(existing.body) !== canonical(body))) throw new RunApiError(409, "IDEMPOTENCY_CONFLICT");
  // Recheck after async identity lookup. Multiple callers share one promise.
  const intent = pending.get(owner) || existing || { owner, key: crypto.randomUUID(), path, body: structuredClone(body) };
  if (intent.path !== path || canonical(intent.body) !== canonical(body)) throw new RunApiError(409, "IDEMPOTENCY_CONFLICT");
  pending.set(owner, intent);
  try { sessionStorage.setItem(`qf.s3.pending.${owner}`, JSON.stringify(intent)); } catch { /* Never discard the in-memory key. */ }
  const active = inFlight.get(owner); if (active) return active as Promise<RunSummary | BatchPage>;
  const operation = request<RunSummary | BatchPage>(path, { method: "POST", headers: { "Idempotency-Key": intent.key }, body: JSON.stringify(intent.body) })
    .then(value => {
      if ("items" in value) page(value); else run(value);
      pending.delete(owner); try { sessionStorage.removeItem(`qf.s3.pending.${owner}`); } catch { /* optional storage */ }
      return value;
    }).finally(() => { inFlight.delete(owner); });
  inFlight.set(owner, operation); return operation;
}
export async function retryPending(): Promise<RunSummary | BatchPage> {
  const intent = await pendingSubmission(); if (!intent) throw new RunApiError(409, "INVALID_CONTRACT");
  return submit(intent.body as RunConfig | { configs: RunConfig[] });
}
