import { readApiToken } from "../auth/tokenStorage";

export type TaskState = "active" | "paused" | "completed" | "archived";
export type OverlapPolicy = "skip" | "queue";
export type RunStatus = "queued" | "running" | "succeeded" | "failed" | "skipped" | "interrupted" | "cancelled" | "timed_out" | "indeterminate";

export type TaskSchedule =
  | { type: "cron"; expression: string; timezone: string }
  | { type: "interval"; seconds: number; start_at: string }
  | { type: "once"; run_at: string };

export interface SchedulerTask {
  id: string;
  name: string;
  description: string | null;
  task_type: string;
  parameters: Record<string, unknown>;
  parameter_version: number;
  schedule: TaskSchedule;
  state: TaskState;
  concurrency_limit: number;
  overlap_policy: OverlapPolicy;
  queue_limit: number;
  priority: number;
  version: number;
  next_run_at: string | null;
  latest_run: TaskRun | null;
  created_at: string;
  updated_at: string;
}

export interface TaskRun {
  id: string;
  task_id: string;
  task_version: number;
  task_type: string;
  trigger_type: "scheduled" | "manual";
  status: RunStatus;
  parameters: Record<string, unknown>;
  parameter_version: number;
  priority: number;
  result: Record<string, unknown> | null;
  error_type: string | null;
  error_message: string | null;
  scheduled_at: string | null;
  available_at: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  current_trading_date: string | null;
  current_step: string | null;
  progress: number;
  last_heartbeat_at: string | null;
  worker_id: string | null;
  exit_code: number | null;
  completion_marker: string | null;
  failure_phase: string | null;
  cancellation_requested_at: string | null;
}

export interface TaskType {
  key: string;
  name: string;
  english_name: string | null;
  parameter_version: number;
  parameter_schema: Record<string, unknown>;
  source_key?: string | null;
}

export interface TaskPayload {
  name: string;
  description?: string | null;
  task_type: string;
  parameters: Record<string, unknown>;
  schedule: TaskSchedule;
  concurrency_limit: number;
  overlap_policy: OverlapPolicy;
  queue_limit: number;
  priority: number;
}

export class SchedulerApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "SchedulerApiError";
  }
}

function headers(): HeadersInit {
  const token = readApiToken();
  return token ? { Authorization: `Bearer ${token}`, "Content-Type": "application/json" } : { "Content-Type": "application/json" };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  init?.signal?.addEventListener("abort", abort, { once: true });
  if (init?.signal?.aborted) abort();
  const timer = setTimeout(abort, 20000);
  try {
  const response = await fetch(path, { ...init, cache: "no-store", signal: controller.signal, headers: { ...headers(), ...init?.headers } });
  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）。`;
    try {
      const body = await response.json() as { detail?: unknown };
      if (typeof body.detail === "string" && /[\u4e00-\u9fff]/.test(body.detail) && body.detail.length < 500) message = body.detail;
      else if (Array.isArray(body.detail)) message = "提交内容校验失败，请检查任务配置。";
    } catch {
      // Keep the status fallback when the response body is not JSON.
    }
    if (response.status === 409) message = "任务状态或版本已变化，请刷新后核对配置再操作。";
    if (response.status === 404) message = "任务已不存在，请刷新任务列表。";
    if (response.status === 503) message = "调度服务暂不可用，请稍后刷新核对任务状态。";
    if (response.status === 422) message = "任务配置不符合要求，请检查脚本参数、日期与执行计划。";
    throw new SchedulerApiError(message, response.status);
  }
  if (response.status === 204) return undefined as T;
  return await response.json() as T;
  } catch (error) {
    if (init?.signal?.aborted) throw new DOMException("Request aborted", "AbortError");
    if (error instanceof SchedulerApiError) throw error;
    throw new SchedulerApiError(init?.method && init.method !== "GET"
      ? "未能确认操作结果，请刷新并核对任务及运行历史，不要重复提交。"
      : "任务数据加载失败，请检查网络后刷新重试。", 0);
  } finally {
    clearTimeout(timer); init?.signal?.removeEventListener("abort", abort);
  }
}

export function listTasks(): Promise<SchedulerTask[]> {
  return request<SchedulerTask[]>("/api/admin/tasks");
}

export function listTaskTypes(signal?: AbortSignal): Promise<TaskType[]> {
  return request<TaskType[]>("/api/admin/task-types", { signal });
}

export function listTaskRuns(taskId: string, offset = 0, signal?: AbortSignal): Promise<TaskRun[]> {
  return request<TaskRun[]>(`/api/admin/tasks/${encodeURIComponent(taskId)}/runs?limit=10&offset=${offset}`, { signal });
}

export type WorkspaceStatus = "all" | "active" | "paused" | "completed" | "running" | "queued" | "attention";
export interface WorkspaceTask extends SchedulerTask {
  registered: boolean; task_type_name: string | null; task_type_english_name: string | null;
  source_key: string | null; source_enabled: boolean | null; source_configured: boolean | null;
  running_count: number; queued_count: number;
}
export interface TaskWorkspace { items: WorkspaceTask[]; total: number; limit: number; offset: number }
export function listTaskWorkspace(options: { query?: string; source_key?: string; status?: WorkspaceStatus; offset?: number }, signal?: AbortSignal): Promise<TaskWorkspace> {
  const query = new URLSearchParams({ limit: "20", offset: String(options.offset ?? 0), status: options.status ?? "all" });
  if (options.query) query.set("query", options.query);
  if (options.source_key) query.set("source_key", options.source_key);
  return request<TaskWorkspace>(`/api/admin/task-workspace?${query}`, { signal });
}

export function listRecentTaskRuns(): Promise<TaskRun[]> {
  return request<TaskRun[]>("/api/admin/task-runs?limit=100");
}

export function createTask(payload: TaskPayload): Promise<SchedulerTask> {
  return request<SchedulerTask>("/api/admin/tasks", { method: "POST", body: JSON.stringify(payload) });
}

export function updateTask(task: SchedulerTask, payload: Partial<TaskPayload>): Promise<SchedulerTask> {
  return request<SchedulerTask>(`/api/admin/tasks/${task.id}`, {
    method: "PATCH",
    body: JSON.stringify({ version: task.version, ...payload })
  });
}

export function changeTaskState(task: SchedulerTask): Promise<SchedulerTask> {
  const action = task.state === "active" ? "pause" : "resume";
  return request<SchedulerTask>(`/api/admin/tasks/${task.id}/${action}?version=${task.version}`, { method: "POST" });
}

export function runTaskNow(taskId: string): Promise<TaskRun> {
  return request<TaskRun>(`/api/admin/tasks/${taskId}/run`, { method: "POST" });
}
