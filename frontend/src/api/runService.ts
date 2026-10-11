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
export interface RunIssue { code: string; operation?: string; message?: string; scope?: Record<string, unknown> }
export interface RunSummary {
  run_id: string; engine_kind: "rust" | "legacy_python"; api_schema: string; status: string;
  cancel_requested?: boolean; partial?: boolean; error?: RunIssue | null;
  progress_completed?: number; progress_total?: number | null;
  progress?: { completed_events: number; completed_sessions: number; now_ns: string } | null;
  normalized_config?: RunConfig;
  created_at?: string; finished_at?: string | null; batch_id?: string | null; retry_of?: string | null;
  account_version?: number; cost_configuration?: Record<string, unknown>;
  strategy_revision_id?: string; account_profile_id?: string; account_profile_version?: string;
}
export interface RunPage { items: RunSummary[]; has_more: boolean; next_cursor: string | null }
export interface BatchPage extends RunPage { batch_id: string }
export interface RetryReceipt { batch_id: string; run_ids: string[] }
export type SubmissionResult = RunSummary | BatchPage | RetryReceipt;
export interface Capabilities {
  api_schema: "qf.backtest.v2";
  engine: { installed: boolean; frequencies: Frequency[]; execution_models: ExecutionModel[]; result_kinds: string[] };
  actual_data: { binding: string; frequency?: Frequency; available: boolean; synthetic?: boolean; reason?: string; limitations?: string[] }[];
  limits: { batch_runs: number; parameter_bytes: number; parameter_depth: number; compare_runs: number };
}
export interface Preflight { errors: RunIssue[]; warnings: string[]; normalized_config: RunConfig | null }
export interface RunResultSummary { schema_id: string; run_id: string; partial: boolean; performance?: Record<string, unknown>; summary?: Record<string, unknown> | null }
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
  SUBMISSION_STORAGE_UNAVAILABLE: "浏览器无法保存本次提交标识，请允许当前会话存储后重试。尚未发送新的提交。",
  INVALID_CONTRACT: "服务返回的数据不符合运行契约，请稍后重试。",
};
export function issueText(issue: RunIssue): string { return messages[issue.code] || "服务未能完成此操作，请核对配置与服务状态后重试。"; }
export function issueCode(issue: RunIssue): string { return Object.hasOwn(messages, issue.code) ? issue.code : "UNKNOWN_ERROR"; }
export function issueLocation(issue: RunIssue): string {
  const scope = issue.scope;
  if (issue.code !== "STRATEGY_ERROR" || scope?.file !== "strategy.py" || typeof scope.function !== "string" || !/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(scope.function) || !/^[1-9][0-9]{0,6}$/.test(String(scope.line))) return "";
  return `策略回调 ${scope.function} · 第 ${scope.line} 行`;
}
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
/** Result and historical readers share immediate access revocation with runs. */
export const readRunResource = request;
function page(value: RunPage): RunPage {
  if (!value || !Array.isArray(value.items) || typeof value.has_more !== "boolean" ||
    value.has_more && (typeof value.next_cursor !== "string" || !value.next_cursor)) throw new RunApiError(502, "INVALID_CONTRACT");
  value.items.forEach(run);
  return value;
}
const frequencies = ["1d", "1m", "5m", "15m", "30m", "60m", "tick"];
const models = ["bar_next_interval_v1", "trade_tick_v1", "quote_tick_v1"];
function configContract(value: RunConfig | null | undefined): boolean {
  return !!value && value.api_schema === "qf.backtest.v2" &&
    [value.initial_cash, value.strategy_revision_id, value.account_id, value.start, value.end].every(v => typeof v === "string" && !!v) &&
    frequencies.includes(value.frequency) && models.includes(value.execution_model) &&
    Array.isArray(value.universe) && value.universe.length > 0 && value.universe.length <= 10000 && value.universe.every(v => typeof v === "string" && !!v) &&
    !!value.parameters && typeof value.parameters === "object" && !Array.isArray(value.parameters);
}
function run(value: RunSummary): RunSummary {
  if (!value || typeof value.run_id !== "string" || !value.run_id || !["rust", "legacy_python"].includes(value.engine_kind) || typeof value.status !== "string" ||
    value.engine_kind === "rust" && (!configContract(value.normalized_config) || value.api_schema !== "qf.backtest.v2" || !["queued", "starting", "running", "succeeded", "failed", "cancelled"].includes(value.status))) throw new RunApiError(502, "INVALID_CONTRACT");
  return value;
}
const base = "/api/admin/backtests";
export async function capabilities(signal?: AbortSignal) {
  const value = await request<Capabilities>(`${base}/capabilities`, {}, signal);
  if (value.api_schema !== "qf.backtest.v2" || typeof value.engine?.installed !== "boolean" ||
    !Array.isArray(value.engine.frequencies) || !Array.isArray(value.engine.execution_models) || !Array.isArray(value.actual_data) ||
    value.engine.frequencies.some(f => !frequencies.includes(f)) || value.engine.execution_models.some(m => !models.includes(m)) ||
    !Array.isArray(value.engine.result_kinds) || value.actual_data.some(d => !d || typeof d.available !== "boolean") ||
    !Number.isInteger(value.limits?.batch_runs) || value.limits.batch_runs < 1 || value.limits.batch_runs > 256 ||
    !Number.isInteger(value.limits.parameter_bytes) || value.limits.parameter_bytes < 1 || value.limits.parameter_bytes > 65536 ||
    !Number.isInteger(value.limits.parameter_depth) || value.limits.parameter_depth < 1 || value.limits.parameter_depth > 8) throw new RunApiError(502, "INVALID_CONTRACT");
  return value;
}
export async function preflight(config: RunConfig, signal?: AbortSignal) {
  const value = await request<Preflight>(`${base}/preflight`, { method: "POST", body: JSON.stringify(config) }, signal);
  if (!Array.isArray(value.errors) || !Array.isArray(value.warnings) || value.errors.some(i => !i || typeof i.code !== "string") ||
    !value.errors.length && !configContract(value.normalized_config)) throw new RunApiError(502, "INVALID_CONTRACT");
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
export async function resultSummary(id: string, signal?: AbortSignal) {
  const value = await request<RunResultSummary>(`${base}/${encodeURIComponent(id)}/results/summary?limit=1`, {}, signal);
  if (value.run_id !== id || value.schema_id !== "qf.results.v1" || typeof value.partial !== "boolean") throw new RunApiError(502, "INVALID_CONTRACT");
  return value;
}
export function strategyPage(offset: number, signal?: AbortSignal) { return request<StrategyChoice[]>(`/api/admin/strategies?include_archived=false&limit=100&offset=${offset}`, {}, signal); }
export function revisions(id: string, signal?: AbortSignal) { return request<{ published_revisions: RevisionChoice[] }>(`/api/admin/strategies/${encodeURIComponent(id)}/backtests?limit=1`, {}, signal).then(v => v.published_revisions); }
export function accountPage(offset: number, signal?: AbortSignal) { return request<{ items: AccountChoice[]; total: number }>(`/api/admin/backtest-account-profiles/page?status=active&limit=20&offset=${offset}`, {}, signal); }

export interface Submission { key: string; path: string; body: unknown; owner: string; attempts?: number }
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
function submissionPath(path: string) { return [base, `${base}/batches`].includes(path) || /^\/api\/admin\/backtests\/batches\/[a-f0-9-]+\/retry-failed$/.test(path); }
export async function pendingSubmission(): Promise<Submission | null> {
  const owner = await ownerKey();
  let stored: Submission | null = pending.get(owner) || null;
  try { stored ||= JSON.parse(sessionStorage.getItem(`qf.s3.pending.${owner}`) || "null"); } catch { /* The mounted session still retains its key. */ }
  if (!stored || stored.owner !== owner || !submissionPath(stored.path) || typeof stored.key !== "string") return null;
  pending.set(owner, stored); return stored;
}
export async function submit(body: RunConfig | { configs: RunConfig[] }): Promise<RunSummary | BatchPage> {
  return submitIntent("configs" in body ? `${base}/batches` : base, body) as Promise<RunSummary | BatchPage>;
}
export function retryFailed(id: string): Promise<RetryReceipt> { return submitIntent(`${base}/batches/${encodeURIComponent(id)}/retry-failed`, {}) as Promise<RetryReceipt>; }
async function submitIntent(path: string, body: unknown): Promise<SubmissionResult> {
  const token = readApiToken(), owner = await ownerKey();
  const existing = await pendingSubmission();
  if (existing && (existing.path !== path || canonical(existing.body) !== canonical(body))) throw new RunApiError(409, "IDEMPOTENCY_CONFLICT");
  // Recheck after async identity lookup. Multiple callers share one promise.
  const intent = pending.get(owner) || existing || { owner, key: crypto.randomUUID(), path, body: structuredClone(body) };
  if (intent.path !== path || canonical(intent.body) !== canonical(body)) throw new RunApiError(409, "IDEMPOTENCY_CONFLICT");
  if (token !== readApiToken()) throw new RunApiError(401, "DATA_RESTRICTED");
  const active = inFlight.get(owner); if (active) return active as Promise<SubmissionResult>;
  const attempt = (intent.attempts ?? (existing ? 1 : 0)) + 1;
  const sending = { ...intent, attempts: attempt };
  // A refresh must retain an accepted-but-unconfirmed operation. Fail before
  // sending if the browser cannot persist its key, rather than lose it on reload.
  try { sessionStorage.setItem(`qf.s3.pending.${owner}`, JSON.stringify(sending)); }
  catch { throw new RunApiError(0, "SUBMISSION_STORAGE_UNAVAILABLE"); }
  pending.set(owner, sending);
  const clear = () => { pending.delete(owner); try { sessionStorage.removeItem(`qf.s3.pending.${owner}`); } catch { /* optional storage */ } };
  const operation = request<SubmissionResult>(path, { method: "POST", headers: { "Idempotency-Key": intent.key }, body: JSON.stringify(intent.body) })
    .then(value => {
      if ("items" in value) page(value);
      else if ("run_ids" in value) { if (typeof value.batch_id !== "string" || !Array.isArray(value.run_ids) || value.run_ids.some(id => typeof id !== "string")) throw new RunApiError(502, "INVALID_CONTRACT"); }
      else run(value);
      clear();
      return value;
    }).catch(error => {
      // A first definitive rejection did not admit the operation. A retry's
      // rejection cannot disprove admission of the earlier uncertain request.
      if (attempt === 1 && error instanceof RunApiError && [400, 401, 403, 404, 410, 413, 422].includes(error.status)) clear();
      throw error;
    }).finally(() => { inFlight.delete(owner); });
  inFlight.set(owner, operation); return operation;
}
export async function retryPending(): Promise<SubmissionResult> {
  const intent = await pendingSubmission(); if (!intent) throw new RunApiError(409, "INVALID_CONTRACT");
  return submitIntent(intent.path, intent.body);
}
