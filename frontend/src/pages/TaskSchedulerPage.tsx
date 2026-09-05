import {
  CalendarClock, CircleAlert, CircleCheck, Clock3, LoaderCircle,
  Pause, Play, Plus, RefreshCw, Settings2, X
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { useNavigate } from "react-router-dom";

import {
  changeTaskState, createTask, listRecentTaskRuns, listTaskRuns, listTaskTypes, listTasks,
  runTaskNow, SchedulerApiError, SchedulerTask, TaskPayload, TaskRun,
  TaskSchedule, TaskType, updateTask
} from "../api/scheduler";
import { useAuth } from "../auth/AuthContext";

type TaskFilter = "all" | "active" | "paused";
type ScheduleKind = TaskSchedule["type"];
type CronScheduleMode = "daily" | "weekdays" | "weekly" | "monthly" | "advanced";
type JsonSchema = Record<string, unknown>;

interface TaskDraft {
  name: string; description: string; taskType: string; parameters: string;
  scheduleKind: ScheduleKind; cronExpression: string; timezone: string;
  cronMode: CronScheduleMode; cronTime: string; cronWeekdays: string[]; cronMonthDay: string;
  intervalSeconds: string; startAt: string; runAt: string;
  concurrencyLimit: string; overlapPolicy: "skip" | "queue"; queueLimit: string; priority: string;
}

const WEEKDAYS = [
  { value: "0", label: "周一" }, { value: "1", label: "周二" }, { value: "2", label: "周三" },
  { value: "3", label: "周四" }, { value: "4", label: "周五" }, { value: "5", label: "周六" },
  { value: "6", label: "周日" }
] as const;

/**
 * APScheduler's CronTrigger uses Monday=0 and Sunday=6. Keeping this mapping
 * next to the UI labels prevents an otherwise very easy one-day schedule shift.
 */
function cronExpressionFromDraft(draft: TaskDraft): string {
  if (draft.cronMode === "advanced") return draft.cronExpression.trim();

  const [hour, minute] = draft.cronTime.split(":").map(Number);
  if (!Number.isInteger(hour) || !Number.isInteger(minute) || hour < 0 || hour > 23 || minute < 0 || minute > 59) {
    throw new Error("请选择有效的执行时间。");
  }

  if (draft.cronMode === "daily") return `${minute} ${hour} * * *`;
  if (draft.cronMode === "weekdays") return `${minute} ${hour} * * 0-4`;
  if (draft.cronMode === "weekly") {
    if (draft.cronWeekdays.length === 0) throw new Error("请至少选择一个执行日。");
    return `${minute} ${hour} * * ${[...draft.cronWeekdays].sort((left, right) => Number(left) - Number(right)).join(",")}`;
  }

  const day = Number(draft.cronMonthDay);
  if (!Number.isInteger(day) || day < 1 || day > 31) throw new Error("每月执行日必须在 1 到 31 之间。");
  return `${minute} ${hour} ${day} * *`;
}

function cronEditorValues(expression: string): Pick<TaskDraft, "cronMode" | "cronTime" | "cronWeekdays" | "cronMonthDay"> {
  const defaults = { cronMode: "advanced" as const, cronTime: "18:00", cronWeekdays: ["0"], cronMonthDay: "1" };
  const fields = expression.trim().split(/\s+/);
  if (fields.length !== 5 || !/^\d{1,2}$/.test(fields[0]) || !/^\d{1,2}$/.test(fields[1])) return defaults;

  const minute = Number(fields[0]);
  const hour = Number(fields[1]);
  if (minute > 59 || hour > 23) return defaults;
  const cronTime = `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
  const [, , dayOfMonth, month, dayOfWeek] = fields;

  if (dayOfMonth === "*" && month === "*" && dayOfWeek === "*") return { ...defaults, cronMode: "daily", cronTime };
  if (dayOfMonth === "*" && month === "*" && dayOfWeek === "0-4") return { ...defaults, cronMode: "weekdays", cronTime };
  if (dayOfMonth === "*" && month === "*" && /^([0-6])(,[0-6])*$/.test(dayOfWeek)) {
    return { ...defaults, cronMode: "weekly", cronTime, cronWeekdays: dayOfWeek.split(",") };
  }
  if (/^(?:[1-9]|[12]\d|3[01])$/.test(dayOfMonth) && month === "*" && dayOfWeek === "*") {
    return { ...defaults, cronMode: "monthly", cronTime, cronMonthDay: dayOfMonth };
  }
  return { ...defaults, cronTime };
}

function cronPreview(draft: TaskDraft): string {
  if (draft.cronMode === "advanced") return draft.cronExpression || "请输入 Cron 表达式";
  try {
    return cronExpressionFromDraft(draft);
  } catch (error) {
    return error instanceof Error ? error.message : "调度配置无效";
  }
}

function localDateTimeValue(value?: string): string {
  const date = value ? new Date(value) : new Date(Date.now() + 60_000);
  const valid = Number.isNaN(date.getTime()) ? new Date(Date.now() + 60_000) : date;
  return new Date(valid.getTime() - valid.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

function schemaValue(schema: JsonSchema): unknown {
  if (Object.hasOwn(schema, "default")) return schema.default;
  const alternatives = Array.isArray(schema.anyOf) ? schema.anyOf : [];
  const nonNullAlternative = alternatives.find((item): item is JsonSchema => (
    typeof item === "object" && item !== null && (item as JsonSchema).type !== "null"
  ));
  if (nonNullAlternative) return schemaValue(nonNullAlternative);
  if (Array.isArray(schema.enum) && schema.enum.length > 0) return schema.enum[0];
  if (schema.type === "object") return schemaObjectValue(schema);
  if (schema.type === "array") return [];
  if (schema.type === "boolean") return false;
  if (schema.type === "integer" || schema.type === "number") {
    return typeof schema.minimum === "number" ? schema.minimum : 0;
  }
  return "";
}

function schemaObjectValue(schema: JsonSchema): Record<string, unknown> {
  const properties = schema.properties;
  if (!properties || typeof properties !== "object" || Array.isArray(properties)) return {};
  const required = new Set(
    Array.isArray(schema.required) ? schema.required.filter((item): item is string => typeof item === "string") : []
  );
  return Object.fromEntries(
    Object.entries(properties)
      .filter(([, value]) => typeof value === "object" && value !== null && !Array.isArray(value))
      .filter(([key, value]) => required.has(key) || Object.hasOwn(value as JsonSchema, "default"))
      .map(([key, value]) => [key, schemaValue(value as JsonSchema)])
  );
}

function parametersTemplate(taskType: TaskType | undefined): string {
  return JSON.stringify(schemaObjectValue(taskType?.parameter_schema ?? {}), null, 2);
}

function taskTypeLabel(taskType: TaskType): string {
  return taskType.english_name ? `${taskType.name}（${taskType.english_name}）` : taskType.name;
}

/**
 * Resolve a persisted task type key to the stable user-facing bilingual label.
 * Unknown keys can occur while a task-type registry is being upgraded; keep
 * those internal identifiers out of ordinary UI text instead of leaking them
 * as a fallback.
 */
function taskTypeLabelByKey(taskTypeKey: string, taskTypes: TaskType[]): string {
  const taskType = taskTypes.find((candidate) => candidate.key === taskTypeKey);
  return taskType ? taskTypeLabel(taskType) : "未注册任务类型";
}

function newTaskDraft(types: TaskType[]): TaskDraft {
  const selectedType = types[0];
  return {
    name: "", description: "", taskType: selectedType?.key ?? "",
    parameters: parametersTemplate(selectedType),
    scheduleKind: "cron", cronExpression: "0 18 * * 0-4", timezone: "Asia/Shanghai",
    cronMode: "weekdays", cronTime: "18:00", cronWeekdays: ["0", "1", "2", "3", "4"], cronMonthDay: "1",
    intervalSeconds: "900", startAt: localDateTimeValue(), runAt: localDateTimeValue(),
    concurrencyLimit: "1", overlapPolicy: "skip", queueLimit: "1", priority: "0"
  };
}

function draftFromTask(task: SchedulerTask): TaskDraft {
  const schedule = task.schedule;
  const cronExpression = schedule.type === "cron" ? schedule.expression : "0 18 * * 0-4";
  return {
    name: task.name, description: task.description ?? "", taskType: task.task_type,
    parameters: JSON.stringify(task.parameters, null, 2), scheduleKind: schedule.type,
    cronExpression,
    timezone: schedule.type === "cron" ? schedule.timezone : "Asia/Shanghai",
    ...cronEditorValues(cronExpression),
    intervalSeconds: schedule.type === "interval" ? String(schedule.seconds) : "900",
    startAt: schedule.type === "interval" ? localDateTimeValue(schedule.start_at) : localDateTimeValue(),
    runAt: schedule.type === "once" ? localDateTimeValue(schedule.run_at) : localDateTimeValue(),
    concurrencyLimit: String(task.concurrency_limit), overlapPolicy: task.overlap_policy,
    queueLimit: String(task.queue_limit), priority: String(task.priority)
  };
}

function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}

function formatSchedule(schedule: TaskSchedule): string {
  if (schedule.type === "cron") return `${schedule.expression} · ${schedule.timezone}`;
  if (schedule.type === "interval") return `每 ${schedule.seconds} 秒`;
  return `单次 · ${formatDate(schedule.run_at)}`;
}

function taskStateLabel(state: SchedulerTask["state"]): string {
  return ({ active: "已启用", paused: "已暂停", completed: "已完成", archived: "已归档" })[state];
}

function runStateLabel(status: TaskRun["status"]): string {
  return ({ queued: "等待执行", running: "正在执行", succeeded: "运行成功", failed: "运行失败", skipped: "已跳过", interrupted: "已中断", cancelled: "已取消", timed_out: "已超时", indeterminate: "状态不确定" })[status];
}

function runDescription(run: TaskRun): string {
  if (run.error_message) return run.error_message;
  if (run.result) return JSON.stringify(run.result);
  return run.trigger_type === "manual" ? "手动触发" : "按计划触发";
}

function runProgressText(run: TaskRun): string {
  const percent = Math.round(Math.max(0, Math.min(1, run.progress ?? 0)) * 100);
  const parts = [`完成 ${percent}%`];
  if (run.current_trading_date) parts.push(`交易日 ${run.current_trading_date}`);
  if (run.current_step) parts.push(run.current_step);
  return parts.join(" · ");
}

function taskConcurrency(task: SchedulerTask): string {
  return `${task.concurrency_limit} 个并发 · ${task.overlap_policy === "queue" ? `最多排队 ${task.queue_limit} 次` : "跳过重叠"}`;
}

function scheduleFromDraft(draft: TaskDraft): TaskSchedule {
  if (draft.scheduleKind === "cron") return { type: "cron", expression: cronExpressionFromDraft(draft), timezone: draft.timezone.trim() };
  if (draft.scheduleKind === "interval") return { type: "interval", seconds: Number(draft.intervalSeconds), start_at: new Date(draft.startAt).toISOString() };
  return { type: "once", run_at: new Date(draft.runAt).toISOString() };
}

function payloadFromDraft(draft: TaskDraft): TaskPayload {
  const parameters = JSON.parse(draft.parameters) as unknown;
  if (!parameters || Array.isArray(parameters) || typeof parameters !== "object") throw new Error("任务参数必须是 JSON 对象。");
  return {
    name: draft.name.trim(), description: draft.description.trim() || undefined, task_type: draft.taskType,
    parameters: parameters as Record<string, unknown>, schedule: scheduleFromDraft(draft),
    concurrency_limit: Number(draft.concurrencyLimit), overlap_policy: draft.overlapPolicy,
    queue_limit: Number(draft.queueLimit), priority: Number(draft.priority)
  };
}

function RunIcon({ status }: { status: TaskRun["status"] }) {
  return status === "succeeded" ? <CircleCheck aria-hidden="true" /> : <CircleAlert aria-hidden="true" />;
}

export function TaskSchedulerPage() {
  const { logout } = useAuth();
  const navigate = useNavigate();
  const [tasks, setTasks] = useState<SchedulerTask[]>([]);
  const [taskTypes, setTaskTypes] = useState<TaskType[]>([]);
  const [runs, setRuns] = useState<TaskRun[]>([]);
  const [recentRuns, setRecentRuns] = useState<TaskRun[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [filter, setFilter] = useState<TaskFilter>("all");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [modalMode, setModalMode] = useState<"create" | "edit" | null>(null);
  const [draft, setDraft] = useState<TaskDraft>(() => newTaskDraft([]));

  const selectedTask = tasks.find((task) => task.id === selectedId) ?? tasks[0] ?? null;
  const canManuallyRunSelectedTask = selectedTask?.state === "active" || selectedTask?.state === "paused";
  const visibleTasks = useMemo(() => tasks.filter((task) => filter === "all" || task.state === filter), [filter, tasks]);
  const activeCount = tasks.filter((task) => task.state === "active").length;
  const todayCompleted = recentRuns.filter((run) => run.status === "succeeded" && new Date(run.finished_at ?? run.created_at).toDateString() === new Date().toDateString()).length;
  const issueCount = recentRuns.filter((run) => run.status === "failed" || run.status === "interrupted").length;

  const handleError = useCallback((caught: unknown, fallback: string) => {
    if (caught instanceof SchedulerApiError && caught.status === 401) {
      logout(); navigate("/login", { replace: true }); return;
    }
    setError(caught instanceof Error ? caught.message : fallback);
  }, [logout, navigate]);

  const loadTasks = useCallback(async (background = false) => {
    if (background) setRefreshing(true); else setLoading(true);
    setError(null);
    try {
      const [nextTasks, nextTypes, nextRuns] = await Promise.all([listTasks(), listTaskTypes(), listRecentTaskRuns()]);
      setTasks(nextTasks); setTaskTypes(nextTypes); setRecentRuns(nextRuns);
      setSelectedId((current) => nextTasks.some((task) => task.id === current) ? current : nextTasks[0]?.id ?? null);
    } catch (caught) { handleError(caught, "任务加载失败。");
    } finally { setLoading(false); setRefreshing(false); }
  }, [handleError]);

  useEffect(() => { void loadTasks(); }, [loadTasks]);
  useEffect(() => {
    if (!selectedTask) { setRuns([]); return; }
    setRuns([]);
    let cancelled = false;
    listTaskRuns(selectedTask.id).then((nextRuns) => { if (!cancelled) setRuns(nextRuns); }).catch((caught) => { if (!cancelled) handleError(caught, "运行历史加载失败。"); });
    return () => { cancelled = true; };
  }, [handleError, selectedTask?.id]);

  useEffect(() => {
    if (modalMode !== "create") return;
    setDraft((current) => ({
      ...current,
      parameters: parametersTemplate(
        taskTypes.find((taskType) => taskType.key === current.taskType)
      )
    }));
  }, [draft.taskType, modalMode, taskTypes]);

  function openCreate() { setDraft(newTaskDraft(taskTypes)); setModalMode("create"); setError(null); }
  function openEdit() { if (selectedTask) { setDraft(draftFromTask(selectedTask)); setModalMode("edit"); setError(null); } }

  async function submitTask(event: FormEvent) {
    event.preventDefault(); setSaving(true); setError(null);
    try {
      const payload = payloadFromDraft(draft);
      const task = modalMode === "edit" && selectedTask ? await updateTask(selectedTask, payload) : await createTask(payload);
      setModalMode(null); setSelectedId(task.id); await loadTasks(true);
    } catch (caught) { handleError(caught, "保存任务失败。");
    } finally { setSaving(false); }
  }

  async function toggleTask(task: SchedulerTask) {
    setError(null);
    try { const changed = await changeTaskState(task); setTasks((current) => current.map((item) => item.id === changed.id ? changed : item)); }
    catch (caught) { handleError(caught, "更新任务状态失败。"); }
  }

  async function queueManualRun(task: SchedulerTask) {
    setError(null);
    try {
      const createdRun = await runTaskNow(task.id);
      if (task.id === selectedTask?.id) setRuns((current) => [createdRun, ...current]);
      setTasks((current) => current.map((item) => (
        item.id === task.id ? { ...item, latest_run: createdRun } : item
      )));
      setRecentRuns((current) => [createdRun, ...current]);
    }
    catch (caught) { handleError(caught, "任务加入执行队列失败。"); }
  }

  return <section className="tasks-page" aria-labelledby="tasks-title">
    <div className="page-heading tasks-page__heading"><div><h2 id="tasks-title">任务调度</h2><p>查看计划任务、执行队列和最近运行结果</p></div><div className="tasks-page__actions"><button className="toolbar-button" type="button" disabled={refreshing} onClick={() => void loadTasks(true)}><RefreshCw className={refreshing ? "spin" : ""} aria-hidden="true" />刷新</button><button className="task-create-button" type="button" disabled={loading || taskTypes.length === 0} title={taskTypes.length === 0 ? "请先在后端注册任务类型" : undefined} onClick={openCreate}><Plus aria-hidden="true" />新建任务</button></div></div>
    {error && <div className="task-message task-message--error" role="alert"><CircleAlert aria-hidden="true" />{error}</div>}
    {!loading && taskTypes.length === 0 && <div className="task-message" role="status">当前没有已注册的任务类型。请先在后端注册业务任务，随后即可在这里创建计划。</div>}
    <div className="scheduler-stats" aria-label="调度概览"><article className="scheduler-stat"><span>启用任务</span><strong>{activeCount}</strong><small>计划会自动触发</small></article><article className="scheduler-stat scheduler-stat--queue"><span>等待执行</span><strong>{recentRuns.filter((run) => run.status === "queued").length}</strong><small>当前服务的执行队列</small></article><article className="scheduler-stat"><span>今日已完成</span><strong>{todayCompleted}</strong><small>全部任务的运行记录</small></article><article className="scheduler-stat scheduler-stat--alert"><span>需要处理</span><strong>{issueCount}</strong><small>失败或中断的最近运行</small></article></div>
    <div className="scheduler-layout"><div className="task-board"><div className="task-board__toolbar"><div className="task-filter-group" role="group" aria-label="任务状态筛选">{(["all", "active", "paused"] as const).map((value) => <button className={`task-filter${filter === value ? " task-filter--active" : ""}`} key={value} type="button" onClick={() => setFilter(value)}>{value === "all" ? "全部" : taskStateLabel(value)}<span>{value === "all" ? tasks.length : tasks.filter((task) => task.state === value).length}</span></button>)}</div><span className="task-board__updated"><Clock3 aria-hidden="true" />{loading ? "正在加载" : "数据来自调度服务"}</span></div><div className="task-table" role="list" aria-label="计划任务列表"><div className="task-table__header" aria-hidden="true"><span>任务</span><span>调度规则</span><span>下次执行</span><span>最近运行</span></div>{loading ? <div className="task-empty"><LoaderCircle className="spin" aria-hidden="true" />正在加载任务…</div> : visibleTasks.length === 0 ? <div className="task-empty">暂无任务。新建一个计划任务开始使用。</div> : visibleTasks.map((task) => { const latestRun = task.latest_run; return <article className={`task-row${task.id === selectedTask?.id ? " task-row--selected" : ""}`} key={task.id} role="listitem" onClick={() => setSelectedId(task.id)}><div className="task-row__name"><span className={`task-state-dot task-state-dot--${task.state}`} aria-label={taskStateLabel(task.state)} /><div><strong>{task.name}</strong><small>{task.description || taskTypeLabelByKey(task.task_type, taskTypes)}</small></div></div><div className="task-row__schedule"><CalendarClock aria-hidden="true" /><span>{formatSchedule(task.schedule)}</span></div><div className="task-row__next"><strong>{task.state === "active" ? formatDate(task.next_run_at) : taskStateLabel(task.state)}</strong><small>{taskConcurrency(task)}</small></div><div className={`run-state run-state--${latestRun?.status ?? "queued"}`}><RunIcon status={latestRun?.status ?? "queued"} /><div><strong>{latestRun ? runStateLabel(latestRun.status) : "暂无运行记录"}</strong><small>{latestRun ? formatDate(latestRun.finished_at ?? latestRun.started_at ?? latestRun.created_at) : "—"}</small></div></div></article>; })}</div></div>
      <aside className="task-detail" aria-labelledby="task-detail-title">{!selectedTask ? <div className="task-empty">选择或新建一个任务查看详情。</div> : <><div className="task-detail__heading"><div><span className={`task-detail__state task-detail__state--${selectedTask.state}`}>{taskStateLabel(selectedTask.state)}</span><h3 id="task-detail-title">{selectedTask.name}</h3><p>{taskTypeLabelByKey(selectedTask.task_type, taskTypes)}</p></div><button className="icon-button" type="button" title="编辑任务" onClick={openEdit}><Settings2 aria-hidden="true" /></button></div><dl className="task-detail__facts"><div><dt>下一次执行</dt><dd>{formatDate(selectedTask.next_run_at)}</dd></div><div><dt>调度规则</dt><dd>{formatSchedule(selectedTask.schedule)}</dd></div><div><dt>并发策略</dt><dd>{taskConcurrency(selectedTask)}</dd></div><div><dt>优先级</dt><dd>{selectedTask.priority}</dd></div></dl><div className="task-detail__run-actions">{canManuallyRunSelectedTask ? <><button className="task-create-button" type="button" onClick={() => void queueManualRun(selectedTask)}><Play aria-hidden="true" />立即运行</button><button className="toolbar-button" type="button" onClick={() => void toggleTask(selectedTask)}>{selectedTask.state === "active" ? <Pause aria-hidden="true" /> : <Play aria-hidden="true" />}{selectedTask.state === "active" ? "暂停任务" : "恢复任务"}</button></> : selectedTask.state === "completed" ? <><button className="task-create-button" type="button" onClick={openEdit}><CalendarClock aria-hidden="true" />重新设置调度</button><p className="task-detail__run-hint" role="status">单次任务已完成。设置未来执行时间并保存后，才可再次运行。</p></> : <p className="task-detail__run-hint" role="status">已归档任务不能运行。</p>}</div><div className="task-detail__history"><div className="task-detail__section-title"><h4>最近运行</h4><span>{runs.length} 条</span></div><ol>{runs.length === 0 ? <li className="task-history-empty">暂无运行记录</li> : runs.slice(0, 5).map((run) => <li key={run.id}><span className={`run-history__icon run-history__icon--${run.status}`}><RunIcon status={run.status} /></span><div><strong>{runStateLabel(run.status)}</strong><span>{runDescription(run)}</span><small>{runProgressText(run)}{run.last_heartbeat_at ? ` · 心跳 ${formatDate(run.last_heartbeat_at)}` : ""}</small></div><time>{formatDate(run.finished_at ?? run.started_at ?? run.created_at)}<small>{run.trigger_type === "manual" ? "手动" : "计划"}</small></time></li>)}</ol></div></>}</aside></div>
    {modalMode && <div className="prototype-modal" role="presentation" onMouseDown={() => !saving && setModalMode(null)}><form className="prototype-modal__dialog task-form" onSubmit={submitTask} onMouseDown={(event) => event.stopPropagation()}><div className="prototype-modal__heading"><div><span>{modalMode === "create" ? "创建任务" : "编辑任务"}</span><h3>{modalMode === "create" ? "新建计划任务" : "更新计划任务"}</h3></div><button className="icon-button" type="button" disabled={saving} onClick={() => setModalMode(null)}><X aria-hidden="true" /></button></div><label>任务名称<input required maxLength={100} value={draft.name} onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))} placeholder="例如：盘后信号通知" /></label><label>任务说明<input maxLength={10000} value={draft.description} onChange={(event) => setDraft((current) => ({ ...current, description: event.target.value }))} placeholder="可选，说明任务用途" /></label><label>任务类型<select value={draft.taskType} onChange={(event) => setDraft((current) => ({ ...current, taskType: event.target.value }))}>{taskTypes.map((type) => <option key={type.key} value={type.key}>{taskTypeLabel(type)}</option>)}</select></label><label>任务参数（JSON）<textarea required value={draft.parameters} onChange={(event) => setDraft((current) => ({ ...current, parameters: event.target.value }))} spellCheck="false" /></label><div className="task-form__grid"><label>调度方式<select value={draft.scheduleKind} onChange={(event) => setDraft((current) => ({ ...current, scheduleKind: event.target.value as ScheduleKind }))}><option value="cron">按日历配置</option><option value="interval">固定间隔</option><option value="once">单次执行</option></select></label>{draft.scheduleKind === "interval" && <><label>间隔秒数<input required type="number" min="1" value={draft.intervalSeconds} onChange={(event) => setDraft((current) => ({ ...current, intervalSeconds: event.target.value }))} /></label><label>开始时间<input required type="datetime-local" value={draft.startAt} onChange={(event) => setDraft((current) => ({ ...current, startAt: event.target.value }))} /></label></>}{draft.scheduleKind === "once" && <label>执行时间<input required type="datetime-local" value={draft.runAt} onChange={(event) => setDraft((current) => ({ ...current, runAt: event.target.value }))} /></label>}</div>{draft.scheduleKind === "cron" && <div className="cron-editor"><fieldset className="cron-editor__frequency"><legend>重复频率</legend><div className="cron-editor__mode-buttons">{([ ["daily", "每天"], ["weekdays", "工作日"], ["weekly", "每周"], ["monthly", "每月"], ["advanced", "高级 Cron"] ] as const).map(([mode, label]) => <button className={draft.cronMode === mode ? "cron-editor__mode-button cron-editor__mode-button--active" : "cron-editor__mode-button"} key={mode} type="button" aria-pressed={draft.cronMode === mode} onClick={() => setDraft((current) => ({ ...current, cronExpression: mode === "advanced" && !(current.cronMode === "weekly" && current.cronWeekdays.length === 0) ? cronExpressionFromDraft(current) : current.cronExpression, cronMode: mode }))}>{label}</button>)}</div></fieldset><div className="task-form__grid"><label>执行时间<input required type="time" step="60" value={draft.cronTime} onChange={(event) => setDraft((current) => ({ ...current, cronTime: event.target.value }))} /></label><label>时区<select required value={draft.timezone} onChange={(event) => setDraft((current) => ({ ...current, timezone: event.target.value }))}><option value="Asia/Shanghai">中国标准时间（Asia/Shanghai）</option><option value="UTC">协调世界时（UTC）</option><option value="America/New_York">美国东部时间</option><option value="Europe/London">英国时间</option></select></label>{draft.cronMode === "monthly" && <label>每月执行日<input required type="number" min="1" max="31" value={draft.cronMonthDay} onChange={(event) => setDraft((current) => ({ ...current, cronMonthDay: event.target.value }))} /></label>}{draft.cronMode === "advanced" && <label className="cron-editor__expression">Cron 表达式<input required value={draft.cronExpression} onChange={(event) => setDraft((current) => ({ ...current, cronExpression: event.target.value }))} /><small>格式：分 时 日 月 星期，例如 <code>0 18 * * 0-4</code> 表示工作日 18:00。</small></label>}</div>{draft.cronMode === "weekly" && <fieldset className="cron-editor__weekday-picker"><legend>执行日</legend><div>{WEEKDAYS.map((weekday) => <label key={weekday.value}><input type="checkbox" checked={draft.cronWeekdays.includes(weekday.value)} onChange={() => setDraft((current) => ({ ...current, cronWeekdays: current.cronWeekdays.includes(weekday.value) ? current.cronWeekdays.filter((value) => value !== weekday.value) : [...current.cronWeekdays, weekday.value] }))} />{weekday.label}</label>)}</div></fieldset>}<div className="cron-editor__preview"><span>计划预览</span><code>{cronPreview(draft)}</code><small>{draft.cronMode === "advanced" ? "使用高级规则时，请确认表达式含义。" : "保存时会自动生成对应的 Cron 表达式。"}</small></div></div>}<div className="task-form__grid"><label>并发数<input required type="number" min="1" max="32" value={draft.concurrencyLimit} onChange={(event) => setDraft((current) => ({ ...current, concurrencyLimit: event.target.value }))} /></label><label>重叠策略<select value={draft.overlapPolicy} onChange={(event) => setDraft((current) => ({ ...current, overlapPolicy: event.target.value as TaskDraft["overlapPolicy"] }))}><option value="skip">跳过重叠</option><option value="queue">加入队列</option></select></label><label>队列上限<input required type="number" min="1" value={draft.queueLimit} onChange={(event) => setDraft((current) => ({ ...current, queueLimit: event.target.value }))} /></label><label>优先级<input required type="number" min="-100" max="100" value={draft.priority} onChange={(event) => setDraft((current) => ({ ...current, priority: event.target.value }))} /></label></div><p>任务类型和参数将由后端校验。保存后，任务会立即同步到当前调度器。</p><div className="prototype-modal__actions"><button className="toolbar-button" type="button" disabled={saving} onClick={() => setModalMode(null)}>取消</button><button className="task-create-button" type="submit" disabled={saving}>{saving && <LoaderCircle className="spin" aria-hidden="true" />}{modalMode === "create" ? "创建任务" : "保存变更"}</button></div></form></div>}
  </section>;
}
