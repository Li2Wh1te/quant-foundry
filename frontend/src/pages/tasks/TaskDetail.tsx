import { useEffect, useRef, useState } from "react";
import { X, Play, Settings2, RefreshCw } from "lucide-react";
import { Link } from "react-router-dom";
import { listTaskRuns } from "../../api/scheduler";
import type { TaskRun, TaskType, WorkspaceTask } from "../../api/scheduler";
import { parameterFields, parameterInput, taskTypeLabel } from "./taskDraft";
import { canRun, formatSchedule, nextRunLabel, runStateLabel, runSummary, taskStateLabel, taskTime } from "./taskPresentation";

export function TaskDetail({ task, type, sourceName, busy, revision, onClose, onEdit, onToggle, onRun, onError }: {
  task: WorkspaceTask; type?: TaskType; sourceName: string; busy: boolean; revision: number;
  onClose: () => void; onEdit: () => void; onToggle: () => void; onRun: () => void; onError: (error: unknown) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [tab, setTab] = useState("info");
  const [runs, setRuns] = useState<TaskRun[]>([]);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const node = dialog.current!;
    const previous = document.activeElement as HTMLElement | null;
    const media = window.matchMedia("(max-width: 980px)");
    const display = () => { node.close(); if (media.matches) node.showModal(); else node.show(); };
    display(); media.addEventListener("change", display);
    return () => { media.removeEventListener("change", display); node.close(); if (previous?.isConnected) previous.focus(); };
  }, []);
  useEffect(() => {
    if (tab !== "runs") return;
    const controller = new AbortController();
    setLoading(true);
    listTaskRuns(task.id, offset, controller.signal).then(value => { if (!controller.signal.aborted) { setRuns(value); setError(null); } }).catch(caught => {
      if (!controller.signal.aborted) { setError("运行历史加载失败，保留上次结果。请重试。"); onError(caught); }
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [task.id, offset, tab, revision, retry, onError]);
  return <dialog ref={dialog} className="qft-detail" aria-labelledby="qft-detail-title" onCancel={event => { event.preventDefault(); onClose(); }} onKeyDown={event => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); onClose(); } }}>
    <header className="qft-detail-head"><div><span className="qfo-eyebrow">TASK DETAIL</span><h2 id="qft-detail-title">{task.name}</h2></div><button className="qfo-icon-btn" type="button" aria-label="关闭任务详情" onClick={onClose}><X aria-hidden="true" /></button></header>
    <div className="qft-detail-tabs" role="group" aria-label="详情内容"><button type="button" aria-pressed={tab === "info"} onClick={() => setTab("info")}>任务详情</button><button type="button" aria-pressed={tab === "runs"} onClick={() => setTab("runs")}>运行记录</button></div>
    <div className="qft-detail-body">
      {tab === "info" ? <>
        <section className="qft-info"><h3>基本信息</h3><dl>
          <div><dt>采集脚本</dt><dd>{type ? taskTypeLabel(type) : "未注册任务类型"}</dd></div><div><dt>数据源</dt><dd>{sourceName}</dd></div>
          <div><dt>任务状态</dt><dd>{taskStateLabel(task.state)}</dd></div><div><dt>执行计划</dt><dd>{formatSchedule(task.schedule)}<small>{task.schedule.type === "cron" ? task.schedule.timezone : "上海时间"}</small></dd></div>
          <div><dt>下次执行</dt><dd>{nextRunLabel(task)}</dd></div><div><dt>活跃运行</dt><dd>运行中 {task.running_count} · 等待中 {task.queued_count}</dd></div>
          <div><dt>并发策略</dt><dd>{task.concurrency_limit} 个并发 · {task.overlap_policy === "skip" ? "跳过重叠" : `最多排队 ${task.queue_limit} 次`}</dd></div><div><dt>优先级</dt><dd>{task.priority}</dd></div>
          {task.description && <div><dt>任务说明</dt><dd>{task.description}</dd></div>}
        </dl></section>
        <section className="qft-info"><h3>采集参数</h3><dl>{parameterFields(type).map(field => <div key={field.key}><dt>{field.label}</dt><dd>{task.parameters[field.key] == null ? "使用默认规则" : field.type === "boolean" ? task.parameters[field.key] ? "已确认" : "未确认" : parameterInput(task.parameters[field.key], field)}</dd></div>)}</dl>{!parameterFields(type).length && <p className="qft-help">无可展示的参数。</p>}</section>
        {!task.registered && <p className="qft-notice">此任务对应的脚本已不在注册列表中，保留历史记录，无法执行或编辑。</p>}
        {task.source_enabled === false && <p className="qft-notice">数据源已停用，任务计划保留。已开始的运行可继续完成。</p>}
        {task.source_configured === false && <p className="qft-notice">请先在数据源页面完成连接配置。</p>}
        {["active", "paused"].includes(task.state) && <div className="qft-toggle-line"><div><strong>启用任务计划</strong><small>暂停仅停止后续调度，不取消已排队或运行中的任务。</small></div><button className={`qft-switch${task.state === "active" ? " qft-on" : ""}`} type="button" role="switch" aria-label="启用任务计划" aria-checked={task.state === "active"} aria-disabled={busy || !task.registered} onClick={() => !busy && task.registered && onToggle()} /></div>}
        {task.state === "completed" && <p className="qft-notice">单次任务已完成。修改执行计划后会转为暂停，请核对后手动启用。</p>}
      </> : <>
        <div className="qft-history-head"><span>上海时间 · 每页 10 条</span><button className="qfo-icon-btn" type="button" aria-label="刷新运行历史" aria-disabled={loading} onClick={() => !loading && setRetry(value => value + 1)}><RefreshCw aria-hidden="true" /></button></div>
        {error && <div className="qft-error" role="alert">{error}</div>}
        {loading && !runs.length ? <p className="qft-empty" role="status">正在加载运行记录…</p> : !runs.length ? <p className="qft-empty">此页暂无运行记录。</p> : <ol className="qft-runs" aria-busy={loading}>{runs.map(run => <li key={run.id}><div className="qft-run-head"><strong>{runStateLabel(run.status)}</strong><time>{taskTime(run.started_at ?? run.created_at)}</time></div><p>{runSummary(run)}</p><small>{run.trigger_type === "manual" ? "手动触发" : "按计划触发"}{run.status === "running" ? ` · 完成 ${Math.round(run.progress * 100)}%` : ""}{run.current_trading_date ? ` · 交易日 ${run.current_trading_date}` : ""}</small><details><summary>技术详情</summary><dl><div><dt>运行 ID</dt><dd>{run.id}</dd></div><div><dt>任务 ID</dt><dd>{run.task_id}</dd></div><div><dt>结束时间</dt><dd>{taskTime(run.finished_at)}</dd></div><div><dt>最近心跳</dt><dd>{taskTime(run.last_heartbeat_at)}</dd></div></dl><pre>{JSON.stringify({ error_type: run.error_type, error_message: run.error_message, current_step: run.current_step, worker_id: run.worker_id, exit_code: run.exit_code, failure_phase: run.failure_phase, result: run.result }, null, 2)}</pre></details></li>)}</ol>}
        <div className="qft-pagination"><button type="button" className="qfo-secondary-btn" disabled={loading || offset === 0} onClick={() => { setRuns([]); setOffset(value => Math.max(0, value - 10)); }}>上一页</button><span>第 {offset / 10 + 1} 页</span><button type="button" className="qfo-secondary-btn" disabled={loading || runs.length < 10} onClick={() => { setRuns([]); setOffset(value => value + 10); }}>下一页</button></div><Link className="qft-log-link" to="/admin/logs">打开运行日志</Link>
      </>}
    </div>
    <footer className="qft-detail-foot"><button className="qfo-secondary-btn" type="button" disabled={!task.registered} aria-disabled={busy} onClick={() => !busy && onEdit()}><Settings2 aria-hidden="true" />编辑</button><button className="qfo-primary-btn" type="button" disabled={!canRun(task)} aria-disabled={busy} title={!canRun(task) ? nextRunLabel(task) : "按真实队列规则触发一次运行"} onClick={() => !busy && onRun()}><Play aria-hidden="true" />立即运行</button></footer>
  </dialog>;
}
