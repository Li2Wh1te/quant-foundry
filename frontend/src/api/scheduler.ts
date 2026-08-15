import { readApiToken } from "../auth/tokenStorage";

export type TaskState = "active" | "paused" | "completed" | "archived";
export type OverlapPolicy = "skip" | "queue";
export type RunStatus = "queued" | "running" | "succeeded" | "failed" | "skipped" | "interrupted";

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
}

export interface TaskType {
  key: string;
  name: string;
  english_name: string | null;
  parameter_version: number;
  parameter_schema: Record<string, unknown>;
}

export interface TaskPayload {
  name: string;
  description?: string;
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
  const response = await fetch(path, { ...init, headers: { ...headers(), ...init?.headers } });
  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）。`;
    try {
      const body = await response.json() as { detail?: unknown };
      if (typeof body.detail === "string") message = body.detail;
      else if (Array.isArray(body.detail)) message = "提交内容校验失败，请检查任务配置。";
    } catch {
      // Keep the status fallback when the response body is not JSON.
    }
    throw new SchedulerApiError(message, response.status);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function listTasks(): Promise<SchedulerTask[]> {
  return request<SchedulerTask[]>("/api/admin/tasks");
}

export function listTaskTypes(): Promise<TaskType[]> {
  return request<TaskType[]>("/api/admin/task-types");
}

export function listTaskRuns(taskId: string): Promise<TaskRun[]> {
  return request<TaskRun[]>(`/api/admin/tasks/${taskId}/runs?limit=20`);
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
