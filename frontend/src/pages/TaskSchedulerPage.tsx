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

interface TaskDraft {
  name: string; description: string; taskType: string; parameters: string;
  scheduleKind: ScheduleKind; cronExpression: string; timezone: string;
  intervalSeconds: string; startAt: string; runAt: string;
  concurrencyLimit: string; overlapPolicy: "skip" | "queue"; queueLimit: string; priority: string;
}

function localDateTimeValue(value?: string): string {
  const date = value ? new Date(value) : new Date(Date.now() + 60_000);
  const valid = Number.isNaN(date.getTime()) ? new Date(Date.now() + 60_000) : date;
  return new Date(valid.getTime() - valid.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

function newTaskDraft(types: TaskType[]): TaskDraft {
  const taskType = types[0]?.key ?? "";
  return {
    name: "", description: "", taskType,
    parameters: "{}",
    scheduleKind: "cron", cronExpression: "0 18 * * 1-5", timezone: "Asia/Shanghai",
    intervalSeconds: "900", startAt: localDateTimeValue(), runAt: localDateTimeValue(),
    concurrencyLimit: "1", overlapPolicy: "skip", queueLimit: "1", priority: "0"
  };
}

function draftFromTask(task: SchedulerTask): TaskDraft {
  const schedule = task.schedule;
  return {
    name: task.name, description: task.description ?? "", taskType: task.task_type,
    parameters: JSON.stringify(task.parameters, null, 2), scheduleKind: schedule.type,
    cronExpression: schedule.type === "cron" ? schedule.expression : "0 18 * * 1-5",
    timezone: schedule.type === "cron" ? schedule.timezone : "Asia/Shanghai",
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
  return ({ queued: "等待执行", running: "正在执行", succeeded: "运行成功", failed: "运行失败", skipped: "已跳过", interrupted: "已中断" })[status];
}

function runDescription(run: TaskRun): string {
  if (run.error_message) return run.error_message;
  if (run.result) return JSON.stringify(run.result);
  return run.trigger_type === "manual" ? "手动触发" : "按计划触发";
}

function taskConcurrency(task: SchedulerTask): string {
  return `${task.concurrency_limit} 个并发 · ${task.overlap_policy === "queue" ? `最多排队 ${task.queue_limit} 次` : "跳过重叠"}`;
}

function scheduleFromDraft(draft: TaskDraft): TaskSchedule {
  if (draft.scheduleKind === "cron") return { type: "cron", expression: draft.cronExpression.trim(), timezone: draft.timezone.trim() };
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
    let cancelled = false;
    listTaskRuns(selectedTask.id).then((nextRuns) => { if (!cancelled) setRuns(nextRuns); }).catch((caught) => { if (!cancelled) handleError(caught, "运行历史加载失败。"); });
    return () => { cancelled = true; };
  }, [handleError, selectedTask?.id]);

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
    try { const createdRun = await runTaskNow(task.id); setRuns((current) => [createdRun, ...current]); setRecentRuns((current) => [createdRun, ...current]); }
    catch (caught) { handleError(caught, "任务加入执行队列失败。"); }
  }

  return <section className="tasks-page" aria-labelledby="tasks-title">
    <div className="page-heading tasks-page__heading"><div><h2 id="tasks-title">任务调度</h2><p>查看计划任务、执行队列和最近运行结果</p></div><div className="tasks-page__actions"><button className="toolbar-button" type="button" disabled={refreshing} onClick={() => void loadTasks(true)}><RefreshCw className={refreshing ? "spin" : ""} aria-hidden="true" />刷新</button><button className="task-create-button" type="button" disabled={loading || taskTypes.length === 0} title={taskTypes.length === 0 ? "请先在后端注册任务类型" : undefined} onClick={openCreate}><Plus aria-hidden="true" />新建任务</button></div></div>
    {error && <div className="task-message task-message--error" role="alert"><CircleAlert aria-hidden="true" />{error}</div>}
    {!loading && taskTypes.length === 0 && <div className="task-message" role="status">当前没有已注册的任务类型。请先在后端注册业务任务，随后即可在这里创建计划。</div>}
    <div className="scheduler-stats" aria-label="调度概览"><article className="scheduler-stat"><span>启用任务</span><strong>{activeCount}</strong><small>计划会自动触发</small></article><article className="scheduler-stat scheduler-stat--queue"><span>等待执行</span><strong>{recentRuns.filter((run) => run.status === "queued").length}</strong><small>当前服务的执行队列</small></article><article className="scheduler-stat"><span>今日已完成</span><strong>{todayCompleted}</strong><small>全部任务的运行记录</small></article><article className="scheduler-stat scheduler-stat--alert"><span>需要处理</span><strong>{issueCount}</strong><small>失败或中断的最近运行</small></article></div>
    <div className="scheduler-layout"><div className="task-board"><div className="task-board__toolbar"><div className="task-filter-group" role="group" aria-label="任务状态筛选">{(["all", "active", "paused"] as const).map((value) => <button className={`task-filter${filter === value ? " task-filter--active" : ""}`} key={value} type="button" onClick={() => setFilter(value)}>{value === "all" ? "全部" : taskStateLabel(value)}<span>{value === "all" ? tasks.length : tasks.filter((task) => task.state === value).length}</span></button>)}</div><span className="task-board__updated"><Clock3 aria-hidden="true" />{loading ? "正在加载" : "数据来自调度服务"}</span></div><div className="task-table" role="list" aria-label="计划任务列表"><div className="task-table__header" aria-hidden="true"><span>任务</span><span>调度规则</span><span>下次执行</span><span>最近运行</span><span>操作</span></div>{loading ? <div className="task-empty"><LoaderCircle className="spin" aria-hidden="true" />正在加载任务…</div> : visibleTasks.length === 0 ? <div className="task-empty">暂无任务。新建一个计划任务开始使用。</div> : visibleTasks.map((task) => { const latestRun = task.id === selectedTask?.id ? runs[0] : undefined; return <article className={`task-row${task.id === selectedTask?.id ? " task-row--selected" : ""}`} key={task.id} role="listitem" onClick={() => setSelectedId(task.id)}><div className="task-row__name"><span className={`task-state-dot task-state-dot--${task.state}`} aria-label={taskStateLabel(task.state)} /><div><strong>{task.name}</strong><small>{task.description || task.task_type}</small></div></div><div className="task-row__schedule"><CalendarClock aria-hidden="true" /><span>{formatSchedule(task.schedule)}</span></div><div className="task-row__next"><strong>{task.state === "active" ? formatDate(task.next_run_at) : taskStateLabel(task.state)}</strong><small>{taskConcurrency(task)}</small></div><div className={`run-state run-state--${latestRun?.status ?? "queued"}`}><RunIcon status={latestRun?.status ?? "queued"} /><div><strong>{latestRun ? runStateLabel(latestRun.status) : "暂无运行记录"}</strong><small>{latestRun ? formatDate(latestRun.finished_at ?? latestRun.started_at ?? latestRun.created_at) : "—"}</small></div></div><div className="task-row__actions" onClick={(event) => event.stopPropagation()}><button className="row-action row-action--run" type="button" onClick={() => void queueManualRun(task)} title="立即运行"><Play aria-hidden="true" /><span>运行</span></button>{(task.state === "active" || task.state === "paused") && <button className="row-action" type="button" onClick={() => void toggleTask(task)} title={task.state === "active" ? "暂停任务" : "恢复任务"}>{task.state === "active" ? <Pause aria-hidden="true" /> : <Play aria-hidden="true" />}<span>{task.state === "active" ? "暂停" : "恢复"}</span></button>}</div></article>; })}</div></div>
      <aside className="task-detail" aria-labelledby="task-detail-title">{!selectedTask ? <div className="task-empty">选择或新建一个任务查看详情。</div> : <><div className="task-detail__heading"><div><span className={`task-detail__state task-detail__state--${selectedTask.state}`}>{taskStateLabel(selectedTask.state)}</span><h3 id="task-detail-title">{selectedTask.name}</h3><p>{selectedTask.task_type}</p></div><button className="icon-button" type="button" title="编辑任务" onClick={openEdit}><Settings2 aria-hidden="true" /></button></div><dl className="task-detail__facts"><div><dt>下一次执行</dt><dd>{formatDate(selectedTask.next_run_at)}</dd></div><div><dt>调度规则</dt><dd>{formatSchedule(selectedTask.schedule)}</dd></div><div><dt>并发策略</dt><dd>{taskConcurrency(selectedTask)}</dd></div><div><dt>优先级</dt><dd>{selectedTask.priority}</dd></div></dl><div className="task-detail__run-actions"><button className="task-create-button" type="button" onClick={() => void queueManualRun(selectedTask)}><Play aria-hidden="true" />立即运行</button>{(selectedTask.state === "active" || selectedTask.state === "paused") && <button className="toolbar-button" type="button" onClick={() => void toggleTask(selectedTask)}>{selectedTask.state === "active" ? <Pause aria-hidden="true" /> : <Play aria-hidden="true" />}{selectedTask.state === "active" ? "暂停任务" : "恢复任务"}</button>}</div><div className="task-detail__history"><div className="task-detail__section-title"><h4>最近运行</h4><span>{runs.length} 条</span></div><ol>{runs.length === 0 ? <li className="task-history-empty">暂无运行记录</li> : runs.slice(0, 5).map((run) => <li key={run.id}><span className={`run-history__icon run-history__icon--${run.status}`}><RunIcon status={run.status} /></span><div><strong>{runStateLabel(run.status)}</strong><span>{runDescription(run)}</span></div><time>{formatDate(run.finished_at ?? run.started_at ?? run.created_at)}<small>{run.trigger_type === "manual" ? "手动" : "计划"}</small></time></li>)}</ol></div></>}</aside></div>
    {modalMode && <div className="prototype-modal" role="presentation" onMouseDown={() => !saving && setModalMode(null)}><form className="prototype-modal__dialog task-form" onSubmit={submitTask} onMouseDown={(event) => event.stopPropagation()}><div className="prototype-modal__heading"><div><span>{modalMode === "create" ? "创建任务" : "编辑任务"}</span><h3>{modalMode === "create" ? "新建计划任务" : "更新计划任务"}</h3></div><button className="icon-button" type="button" disabled={saving} onClick={() => setModalMode(null)}><X aria-hidden="true" /></button></div><label>任务名称<input required maxLength={100} value={draft.name} onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))} placeholder="例如：盘后信号通知" /></label><label>任务说明<input maxLength={10000} value={draft.description} onChange={(event) => setDraft((current) => ({ ...current, description: event.target.value }))} placeholder="可选，说明任务用途" /></label><label>任务类型<select value={draft.taskType} onChange={(event) => setDraft((current) => ({ ...current, taskType: event.target.value }))}>{taskTypes.map((type) => <option key={type.key} value={type.key}>{type.name} · {type.key}</option>)}</select></label><label>任务参数（JSON）<textarea required value={draft.parameters} onChange={(event) => setDraft((current) => ({ ...current, parameters: event.target.value }))} spellCheck="false" /></label><div className="task-form__grid"><label>调度方式<select value={draft.scheduleKind} onChange={(event) => setDraft((current) => ({ ...current, scheduleKind: event.target.value as ScheduleKind }))}><option value="cron">Cron 表达式</option><option value="interval">固定间隔</option><option value="once">单次执行</option></select></label>{draft.scheduleKind === "cron" && <><label>Cron 表达式<input required value={draft.cronExpression} onChange={(event) => setDraft((current) => ({ ...current, cronExpression: event.target.value }))} /></label><label>时区<input required value={draft.timezone} onChange={(event) => setDraft((current) => ({ ...current, timezone: event.target.value }))} /></label></>}{draft.scheduleKind === "interval" && <><label>间隔秒数<input required type="number" min="1" value={draft.intervalSeconds} onChange={(event) => setDraft((current) => ({ ...current, intervalSeconds: event.target.value }))} /></label><label>开始时间<input required type="datetime-local" value={draft.startAt} onChange={(event) => setDraft((current) => ({ ...current, startAt: event.target.value }))} /></label></>}{draft.scheduleKind === "once" && <label>执行时间<input required type="datetime-local" value={draft.runAt} onChange={(event) => setDraft((current) => ({ ...current, runAt: event.target.value }))} /></label>}</div><div className="task-form__grid"><label>并发数<input required type="number" min="1" max="32" value={draft.concurrencyLimit} onChange={(event) => setDraft((current) => ({ ...current, concurrencyLimit: event.target.value }))} /></label><label>重叠策略<select value={draft.overlapPolicy} onChange={(event) => setDraft((current) => ({ ...current, overlapPolicy: event.target.value as TaskDraft["overlapPolicy"] }))}><option value="skip">跳过重叠</option><option value="queue">加入队列</option></select></label><label>队列上限<input required type="number" min="1" value={draft.queueLimit} onChange={(event) => setDraft((current) => ({ ...current, queueLimit: event.target.value }))} /></label><label>优先级<input required type="number" min="-100" max="100" value={draft.priority} onChange={(event) => setDraft((current) => ({ ...current, priority: event.target.value }))} /></label></div><p>任务类型和参数将由后端校验。保存后，任务会立即同步到当前调度器。</p><div className="prototype-modal__actions"><button className="toolbar-button" type="button" disabled={saving} onClick={() => setModalMode(null)}>取消</button><button className="task-create-button" type="submit" disabled={saving}>{saving && <LoaderCircle className="spin" aria-hidden="true" />}{modalMode === "create" ? "创建任务" : "保存变更"}</button></div></form></div>}
  </section>;
}
