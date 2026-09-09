import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { Database, Plus, RefreshCw, Search } from "lucide-react";
import { useAuth } from "../auth/AuthContext";
import { DataSourceApiError, listDataSources } from "../api/dataSources";
import type { DataSource } from "../api/dataSources";
import { changeTaskState, createTask, listTaskTypes, listTaskWorkspace, runTaskNow, SchedulerApiError, updateTask } from "../api/scheduler";
import type { TaskPayload, TaskType, TaskWorkspace, WorkspaceStatus, WorkspaceTask } from "../api/scheduler";
import { TaskEditor } from "./tasks/TaskEditor";
import { TaskDetail } from "./tasks/TaskDetail";
import { SourceFilter } from "./tasks/SourceFilter";
import { canRun, formatSchedule, nextRunLabel, runStateLabel, taskStateLabel, taskTime } from "./tasks/taskPresentation";
import "./TaskScheduler.css";

const FILTERS: [WorkspaceStatus, string][] = [["all", "全部"], ["active", "已启用"], ["paused", "已暂停"], ["completed", "已完成"], ["running", "运行中"], ["queued", "等待中"], ["attention", "需处理"]];
export function TaskSchedulerPage() {
  const { logout } = useAuth();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [types, setTypes] = useState<TaskType[]>([]);
  const [sources, setSources] = useState<DataSource[]>([]);
  const [data, setData] = useState<TaskWorkspace | null>(null);
  const [query, setQuery] = useState("");
  const [filters, setFilters] = useState<{ query: string; source_key: string; status: WorkspaceStatus; offset: number }>({ query: "", source_key: "", status: "all", offset: 0 });
  const [selected, setSelected] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [editor, setEditor] = useState<{ task: WorkspaceTask | null; initialType?: string } | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [blocked, setBlocked] = useState(false);
  const actionLock = useRef(false);
  const requestRef = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const committedMessage = useRef<string | null>(null);
  const selectedTask = data?.items.find(item => item.id === selected);
  const authError = useCallback((caught: unknown) => {
    if ((caught instanceof SchedulerApiError || caught instanceof DataSourceApiError) && caught.status === 401) { logout(); navigate("/login", { replace: true }); }
  }, [logout, navigate]);
  const load = useCallback(async () => {
    requestRef.current?.abort();
    const controller = new AbortController(); requestRef.current = controller;
    setLoading(true);
    try {
      const [next, nextTypes, nextSources] = await Promise.all([listTaskWorkspace(filters, controller.signal), listTaskTypes(controller.signal), listDataSources(controller.signal)]);
      if (controller.signal.aborted || !mounted.current) return;
      if (filters.offset && !next.items.length) { setFilters(current => ({ ...current, offset: next.total ? Math.floor((next.total - 1) / 20) * 20 : 0 })); return; }
      committedMessage.current = null;
      setData(next); setTypes(nextTypes); setSources(nextSources.items); setError(null);
    } catch (caught) { if (!controller.signal.aborted && mounted.current) { authError(caught); setError(`${committedMessage.current ? `${committedMessage.current}，但列表刷新失败；请勿重复操作。` : ""}${caught instanceof Error ? caught.message : "任务加载失败，请刷新重试。"}`); } }
    finally { if (!controller.signal.aborted && mounted.current) setLoading(false); }
  }, [filters, authError]);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; requestRef.current?.abort(); }; }, []);
  useEffect(() => { void load(); return () => requestRef.current?.abort(); }, [load, revision]);
  useEffect(() => { const timer = setTimeout(() => setFilters(current => current.query === query.trim() ? current : { ...current, query: query.trim(), offset: 0 }), 300); return () => clearTimeout(timer); }, [query]);
  useEffect(() => { if (!toast) return; const timer = setTimeout(() => setToast(null), 5000); return () => clearTimeout(timer); }, [toast]);
  useEffect(() => {
    // Refresh snapshots without unmounting rows or clobbering an open draft.
    const timer = setInterval(() => { if (document.visibilityState === "visible" && !editor && !error && !actionLock.current) setRevision(value => value + 1); }, 15000);
    return () => clearInterval(timer);
  }, [editor, error]);
  useEffect(() => {
    const key = searchParams.get("create_type");
    if (!key || loading || error) return;
    if (types.some(type => type.key === key)) { setEditor({ task: null, initialType: key }); setFormError(null); setBlocked(false); }
    else setError("该采集脚本当前未注册，请返回数据源刷新后重试。");
    const next = new URLSearchParams(searchParams); next.delete("create_type"); setSearchParams(next, { replace: true });
  }, [loading, error, types, searchParams, setSearchParams]);
  function changeFilter(change: Partial<typeof filters>) { setFilters(current => ({ ...current, ...change, offset: 0 })); setSelected(null); }
  function openEditor(task: WorkspaceTask | null) { if (busy || loading || error) return; setEditor({ task }); setFormError(null); setBlocked(false); }
  async function save(payload: Partial<TaskPayload> | TaskPayload) {
    if (!editor || actionLock.current || blocked) return;
    if (editor.task && !Object.keys(payload).length) { setEditor(null); setToast("配置未变化，无需保存。"); return; }
    actionLock.current = true; setBusy(true); setFormError(null); requestRef.current?.abort(); setLoading(false);
    try {
      const task = editor.task ? await updateTask(editor.task, payload) : await createTask(payload as TaskPayload);
      if (!mounted.current) return;
      committedMessage.current = "任务已保存";
      setEditor(null); setToast(task.state === "paused" ? "任务已保存，当前为暂停状态。核对计划后可手动启用。" : "任务计划已保存。");
      setSelected(task.id);
      // Keep the current page and filters after edits; only a newly created
      // task needs the unfiltered first page to make its result discoverable.
      if (!editor.task) { setFilters({ query: "", source_key: "", status: "all", offset: 0 }); setQuery(""); }
      setRevision(value => value + 1);
    } catch (caught) {
      if (!mounted.current) return;
      authError(caught); setFormError(caught instanceof Error ? caught.message : "保存失败，请检查配置。");
      if (caught instanceof SchedulerApiError && [0, 409, 503].includes(caught.status)) setBlocked(true);
    } finally { actionLock.current = false; if (mounted.current) setBusy(false); }
  }
  async function act(task: WorkspaceTask, action: "toggle" | "run") {
    if (actionLock.current || loading || error) return;
    actionLock.current = true; setBusy(true); requestRef.current?.abort();
    try {
      if (action === "run") { const run = await runTaskNow(task.id); if (mounted.current) setToast(`手动触发已返回：${runStateLabel(run.status)}。请在运行记录中查看进展。`); }
      else { const next = await changeTaskState(task); if (mounted.current) setToast(next.state === "active" ? "任务计划已启用；数据源停用时，计划保留但不会启动新运行。" : "任务计划已暂停；已排队和运行中的任务不受影响。"); }
      if (mounted.current) { committedMessage.current = action === "run" ? "手动触发已受理" : "任务状态已更新"; setRevision(value => value + 1); }
    } catch (caught) { if (mounted.current) { authError(caught); setError(caught instanceof Error ? caught.message : "操作未完成，请刷新核对。"); } }
    finally { actionLock.current = false; if (mounted.current) setBusy(false); }
  }
  const sourceName = (task: WorkspaceTask) => sources.find(source => source.key === task.source_key)?.name ?? (task.source_key ? "未知来源" : "无关联来源");
  return <section className="qfo-content qft-content" aria-labelledby="qft-title">
    <header className="qfo-page-head"><div><div className="qfo-eyebrow">COLLECTION TASKS</div><h1 id="qft-title">采集任务</h1><p className="qfo-page-desc">查看任务状态、执行计划与最近运行结果。</p></div><div className="qfo-page-actions"><button className="qfo-secondary-btn" type="button" aria-label="刷新采集任务" aria-disabled={loading || busy} onClick={() => !loading && !busy && setRevision(value => value + 1)}><RefreshCw aria-hidden="true" />刷新</button><button className="qfo-primary-btn" type="button" disabled={!types.length} aria-disabled={busy} onClick={() => openEditor(null)}><Plus aria-hidden="true" />新建采集任务</button><Link className="qfo-secondary-btn" to="/admin/data-sources"><Database aria-hidden="true" />数据源</Link></div></header>
    {error && <div className="qft-error" role="alert">{error}{data && <span> 当前保留上次成功加载的结果，操作暂不可用。</span>}<button className="qfo-secondary-btn" type="button" disabled={loading || busy} onClick={() => setRevision(value => value + 1)}>重新加载</button></div>}
    <div className="qft-workspace">
      <div className="qft-toolbar"><label className="qft-search"><Search aria-hidden="true" /><input aria-label="搜索任务或脚本" placeholder="搜索任务或脚本" maxLength={200} value={query} onChange={e => setQuery(e.target.value)} /></label><SourceFilter sources={sources} value={filters.source_key} onChange={source_key => changeFilter({ source_key })} />
        <div className="qft-filters" role="group" aria-label="任务状态筛选">{FILTERS.map(([key, label]) => <button type="button" key={key} aria-pressed={filters.status === key} title={key === "attention" ? "最新一次运行失败、中断、超时或结果不确定的任务" : undefined} onClick={() => changeFilter({ status: key })}>{label}</button>)}</div><span className="qft-total" role="status">{loading ? "正在更新…" : data ? `共 ${data.total} 个任务` : "尚未加载"}</span>
      </div>
      <div className={`qft-workspace-body${selectedTask ? " qft-with-detail" : ""}`}>
        <div className="qft-table-column"><div className="qft-table-wrap" tabIndex={0} role="region" aria-label="采集任务表格，可横向滚动" aria-busy={loading}>
          <table className="qft-table"><colgroup><col style={{ width: "28%" }} /><col style={{ width: "11%" }} /><col style={{ width: "17%" }} /><col style={{ width: "10%" }} /><col style={{ width: "14%" }} /><col style={{ width: "14%" }} /><col style={{ width: "74px" }} /></colgroup><thead><tr><th scope="col">任务</th><th scope="col">数据源</th><th scope="col">执行计划</th><th scope="col">状态</th><th scope="col">最近运行</th><th scope="col">下次执行</th><th scope="col"><span className="qft-sr-only">操作</span></th></tr></thead><tbody>
            {data?.items.map(task => <tr key={task.id} className={task.id === selected ? "qft-selected" : ""} onClick={() => setSelected(task.id)}><td><button className="qft-task-name" type="button" aria-expanded={selected === task.id} onClick={() => setSelected(task.id)}>{task.name}</button><small>{task.task_type_name ?? "未注册任务类型"}{task.task_type_english_name && `（${task.task_type_english_name}）`}</small></td><td><strong>{sourceName(task)}</strong>{task.source_enabled === false && <small>已停用</small>}</td><td>{formatSchedule(task.schedule)}<small>{task.schedule.type === "cron" ? task.schedule.timezone : "上海时间"}</small></td><td><span className={`qft-state qft-state-${task.state}`}>{taskStateLabel(task.state)}</span>{(task.running_count > 0 || task.queued_count > 0) && <small>运行 {task.running_count} · 等待 {task.queued_count}</small>}</td><td>{task.latest_run ? <>{taskTime(task.latest_run.started_at ?? task.latest_run.created_at)}<small>{runStateLabel(task.latest_run.status)}</small></> : "暂无运行"}</td><td>{nextRunLabel(task)}</td><td><button className="qft-mini-btn" type="button" disabled={!canRun(task)} aria-disabled={busy || loading || !!error} title={canRun(task) ? "触发一次运行" : nextRunLabel(task)} onClick={event => { event.stopPropagation(); void act(task, "run"); }}>运行</button></td></tr>)}
          </tbody></table>
          {!data && loading ? <div className="qft-empty" role="status">正在加载采集任务…</div> : !data?.items.length && <div className="qft-empty">{error ? "任务暂不可用，请重新加载。" : filters.query || filters.status !== "all" || filters.source_key ? "没有符合筛选条件的任务。" : "暂无采集任务，点击“新建采集任务”开始配置。"}</div>}
        </div><footer className="qft-pagination"><button className="qfo-secondary-btn" type="button" disabled={loading || busy || !data || filters.offset === 0} onClick={() => { setSelected(null); setFilters(current => ({ ...current, offset: Math.max(0, current.offset - 20) })); }}>上一页</button><button className="qfo-secondary-btn" type="button" disabled={loading || busy || !data || filters.offset + 20 >= data.total} onClick={() => { setSelected(null); setFilters(current => ({ ...current, offset: current.offset + 20 })); }}>下一页</button></footer></div>
        {selectedTask && <TaskDetail key={selectedTask.id} task={selectedTask} type={types.find(type => type.key === selectedTask.task_type)} sourceName={sourceName(selectedTask)} busy={busy || loading || !!error} revision={revision} onClose={() => setSelected(null)} onEdit={() => openEditor(selectedTask)} onToggle={() => void act(selectedTask, "toggle")} onRun={() => void act(selectedTask, "run")} onError={authError} />}
      </div>
    </div>
    {toast && <div className="qfo-toast qfo-show qft-toast" role="status">{toast}</div>}
    {editor && <TaskEditor task={editor.task} initialType={editor.initialType} types={types} sources={sources} busy={busy} error={formError} blocked={blocked} onClose={() => { if (!busy) { setEditor(null); setRevision(value => value + 1); } }} onSave={payload => void save(payload)} />}
  </section>;
}
