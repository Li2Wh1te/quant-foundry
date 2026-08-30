import {
  Activity,
  ArrowRight,
  CalendarDays,
  CheckCircle2,
  CircleAlert,
  Clock3,
  Database,
  ListChecks,
  RefreshCw
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import {
  DataCollectionApiError,
  EtfOverview,
  getEtfOverview,
  getTradingCalendarOverview,
  TradingCalendarOverview
} from "../api/dataCollections";
import {
  listRecentTaskRuns,
  listTasks,
  listTaskTypes,
  SchedulerApiError,
  SchedulerTask,
  TaskRun,
  TaskType
} from "../api/scheduler";
import { useAuth } from "../auth/AuthContext";

interface DashboardSnapshot {
  calendar: TradingCalendarOverview;
  etfs: EtfOverview;
  tasks: SchedulerTask[];
  taskTypes: TaskType[];
  recentRuns: TaskRun[];
}

/**
 * The dashboard intentionally aggregates only data already exposed by the
 * administration APIs. It never invents a health score or a market statistic:
 * every visible number can be traced back to a collection or scheduler response.
 */
function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "尚未同步";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false
  }).format(date);
}

function isSameLocalDay(value: string | null | undefined): boolean {
  if (!value) return false;
  const date = new Date(value);
  const now = new Date();
  return !Number.isNaN(date.getTime())
    && date.getFullYear() === now.getFullYear()
    && date.getMonth() === now.getMonth()
    && date.getDate() === now.getDate();
}

function runStatusLabel(run: TaskRun): string {
  return {
    queued: "等待执行",
    running: "正在执行",
    succeeded: "运行成功",
    failed: "运行失败",
    skipped: "已跳过",
    interrupted: "已中断",
    cancelled: "已取消",
    timed_out: "已超时",
    indeterminate: "状态不确定"
  }[run.status];
}

function latestTimestamp(...values: Array<string | null | undefined>): string | null {
  const timestamps = values
    .map((value) => value ? new Date(value).getTime() : Number.NaN)
    .filter((value) => Number.isFinite(value));
  if (timestamps.length === 0) return null;
  return new Date(Math.max(...timestamps)).toISOString();
}

function taskTypeLabel(taskType: TaskType | undefined): string {
  if (!taskType) return "计划任务";
  return taskType.english_name
    ? `${taskType.name}（${taskType.english_name}）`
    : taskType.name;
}

export function DashboardPage() {
  const { logout } = useAuth();
  const navigate = useNavigate();
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadSnapshot = useCallback(async (background = false) => {
    if (background) setRefreshing(true); else setLoading(true);
    setError(null);
    try {
      const [calendar, etfs, tasks, taskTypes, recentRuns] = await Promise.all([
        getTradingCalendarOverview(),
        getEtfOverview(),
        listTasks(),
        listTaskTypes(),
        listRecentTaskRuns()
      ]);
      setSnapshot({ calendar, etfs, tasks, taskTypes, recentRuns });
    } catch (caught) {
      if (
        (caught instanceof DataCollectionApiError || caught instanceof SchedulerApiError)
        && caught.status === 401
      ) {
        logout();
        navigate("/login", { replace: true });
        return;
      }
      setError(caught instanceof Error ? caught.message : "工作区概览加载失败。");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [logout, navigate]);

  useEffect(() => { void loadSnapshot(); }, [loadSnapshot]);

  const taskNames = useMemo(() => {
    if (!snapshot) return new Map<string, string>();
    const typeByKey = new Map(snapshot.taskTypes.map((taskType) => [taskType.key, taskType]));
    return new Map(snapshot.tasks.map((task) => [
      task.id,
      task.name || taskTypeLabel(typeByKey.get(task.task_type))
    ]));
  }, [snapshot]);

  const activeTaskCount = snapshot?.tasks.filter((task) => task.state === "active").length ?? 0;
  const queuedRunCount = snapshot?.recentRuns.filter((run) => run.status === "queued" || run.status === "running").length ?? 0;
  const issueRuns = snapshot?.recentRuns.filter((run) => run.status === "failed" || run.status === "interrupted") ?? [];
  const completedToday = snapshot?.recentRuns.filter((run) => (
    run.status === "succeeded" && isSameLocalDay(run.finished_at ?? run.created_at)
  )).length ?? 0;
  const updatedAt = snapshot && latestTimestamp(
    snapshot.etfs.refreshed_at,
    snapshot.etfs.last_updated_at,
    snapshot.calendar.last_updated_at
  );
  const hasSnapshot = snapshot !== null;

  return (
    <section className="dashboard-page" aria-labelledby="dashboard-title">
      <div className="page-heading dashboard-page__heading">
        <div>
          <span className="workbench-eyebrow">OPERATIONS OVERVIEW</span>
          <h2 id="dashboard-title">数据运营总览</h2>
          <p>从当前数据状态进入下一步操作；所有指标均来自已接入的管理接口。</p>
        </div>
        <div className="dashboard-page__actions">
          <span className="dashboard-updated">{loading ? "正在同步工作区…" : `数据更新于 ${formatTimestamp(updatedAt)}`}</span>
          <button className="toolbar-button" type="button" disabled={refreshing} onClick={() => void loadSnapshot(true)}>
            <RefreshCw className={refreshing ? "spin" : ""} aria-hidden="true" />刷新快照
          </button>
        </div>
      </div>

      {error && <div className="page-error" role="alert"><CircleAlert aria-hidden="true" />{error}</div>}

      <section className="dashboard-signal" aria-label="当前工作区状态">
        <div className="dashboard-signal__summary">
          <span className="workbench-eyebrow">CURRENT FOCUS</span>
          <strong>{loading ? "正在加载运行快照" : issueRuns.length > 0 ? "存在需要处理的运行结果" : "未发现最近失败或中断的运行"}</strong>
          <p>{loading
            ? "正在汇总 ETF、交易日历和调度服务返回的数据。"
            : issueRuns.length > 0
              ? `最近运行中有 ${issueRuns.length} 条失败或中断记录，请优先在运行日志中核查。`
              : "可以继续查看采集覆盖范围，或进入任务调度管理下一轮执行。"}</p>
          <div className="dashboard-signal__actions">
            <Link className="task-create-button" to={issueRuns.length > 0 ? "/admin/logs" : "/admin/data/etf-basics"}>
              {issueRuns.length > 0 ? "查看需要关注的日志" : "查看 ETF 数据"}<ArrowRight aria-hidden="true" />
            </Link>
            <Link className="toolbar-button" to="/admin/tasks">任务调度</Link>
          </div>
        </div>
        <div className="dashboard-signal__data" aria-label="数据概览">
          <div><span>ETF 记录</span><strong>{hasSnapshot ? snapshot.etfs.total_records.toLocaleString("zh-CN") : "—"}</strong><small>基础信息数据库</small></div>
          <div><span>交易日记录</span><strong>{hasSnapshot ? snapshot.calendar.total_records.toLocaleString("zh-CN") : "—"}</strong><small>覆盖 {hasSnapshot ? snapshot.calendar.exchange_count : "—"} 个交易所</small></div>
          <div><span>启用任务</span><strong>{hasSnapshot ? activeTaskCount : "—"}</strong><small>等待队列 {hasSnapshot ? queuedRunCount : "—"} 项</small></div>
        </div>
      </section>

      <div className="dashboard-stats" aria-label="运行摘要">
        <article className="dashboard-stat"><span><Activity aria-hidden="true" />今日已完成</span><strong>{hasSnapshot ? completedToday : "—"}</strong><small>按调度器返回的成功运行统计</small></article>
        <article className="dashboard-stat dashboard-stat--lime"><span><Database aria-hidden="true" />上市 ETF</span><strong>{hasSnapshot ? snapshot.etfs.listed_count.toLocaleString("zh-CN") : "—"}</strong><small>ETF 基础信息中当前上市的标的</small></article>
        <article className="dashboard-stat dashboard-stat--amber"><span><CalendarDays aria-hidden="true" />交易日记录</span><strong>{hasSnapshot ? snapshot.calendar.open_day_count.toLocaleString("zh-CN") : "—"}</strong><small>按交易所分别保存的交易日</small></article>
        <article className={`dashboard-stat${issueRuns.length > 0 ? " dashboard-stat--danger" : ""}`}><span><ListChecks aria-hidden="true" />需要处理</span><strong>{hasSnapshot ? issueRuns.length : "—"}</strong><small>失败或中断的最近运行</small></article>
      </div>

      <div className="dashboard-grid">
        <section className="dashboard-panel" aria-labelledby="dashboard-collection-title">
          <div className="dashboard-panel__heading"><div><span className="workbench-eyebrow">DATA COVERAGE</span><h3 id="dashboard-collection-title">采集边界</h3></div><Link to="/admin/data/trading-calendar">查看数据采集 <ArrowRight aria-hidden="true" /></Link></div>
          <div className="dashboard-collection-list">
            <Link className="dashboard-collection-row" to="/admin/data/etf-basics"><span className="dashboard-collection-row__icon dashboard-collection-row__icon--lime"><Database aria-hidden="true" /></span><span><strong>ETF 基础信息</strong><small>{hasSnapshot ? `数据库 ${snapshot.etfs.total_records.toLocaleString("zh-CN")} 条，上市 ${snapshot.etfs.listed_count.toLocaleString("zh-CN")} 条` : "等待接口返回数据"}</small></span><time>{formatTimestamp(snapshot?.etfs.refreshed_at ?? snapshot?.etfs.last_updated_at)}</time></Link>
            <Link className="dashboard-collection-row" to="/admin/data/trading-calendar"><span className="dashboard-collection-row__icon dashboard-collection-row__icon--amber"><CalendarDays aria-hidden="true" /></span><span><strong>交易日历</strong><small>{hasSnapshot ? `覆盖 ${snapshot.calendar.start_date ?? "—"} 至 ${snapshot.calendar.end_date ?? "—"}` : "等待接口返回数据"}</small></span><time>{formatTimestamp(snapshot?.calendar.last_updated_at)}</time></Link>
            <Link className="dashboard-collection-row" to="/admin/data/daily-quotes"><span className="dashboard-collection-row__icon"><Activity aria-hidden="true" /></span><span><strong>日线行情</strong><small>进入数据页面查看当前接入状态和后续采集能力。</small></span><time>→</time></Link>
          </div>
        </section>

        <section className="dashboard-panel" aria-labelledby="dashboard-runs-title">
          <div className="dashboard-panel__heading"><div><span className="workbench-eyebrow">RECENT RUNS</span><h3 id="dashboard-runs-title">最近运行</h3></div><Link to="/admin/tasks">调度详情 <ArrowRight aria-hidden="true" /></Link></div>
          <div className="dashboard-run-list">
            {loading ? <div className="dashboard-empty">正在读取调度服务…</div> : snapshot && snapshot.recentRuns.length > 0 ? snapshot.recentRuns.slice(0, 5).map((run) => (
              <Link className="dashboard-run-row" to="/admin/tasks" key={run.id}>
                <i className={`dashboard-run-row__dot dashboard-run-row__dot--${run.status}`} aria-hidden="true" />
                <span><strong>{taskNames.get(run.task_id) ?? "计划任务"}</strong><small>{runStatusLabel(run)}{run.trigger_type === "manual" ? " · 手动触发" : " · 按计划触发"}</small></span>
                <time>{formatTimestamp(run.finished_at ?? run.started_at ?? run.created_at)}</time>
              </Link>
            )) : <div className="dashboard-empty"><Clock3 aria-hidden="true" />暂未返回运行记录</div>}
          </div>
        </section>

        <section className="dashboard-panel dashboard-panel--action" aria-labelledby="dashboard-next-title">
          <div className="dashboard-panel__heading"><div><span className="workbench-eyebrow">NEXT ACTION</span><h3 id="dashboard-next-title">下一步操作</h3></div></div>
          <div className="dashboard-action-list">
            <Link to="/admin/tasks"><span><CheckCircle2 aria-hidden="true" /></span><div><strong>维护任务计划</strong><small>新增、编辑或立即运行现有采集任务。</small></div><ArrowRight aria-hidden="true" /></Link>
            <Link to="/admin/logs"><span><CircleAlert aria-hidden="true" /></span><div><strong>核查运行日志</strong><small>按中文事件标题、结果摘要和时间范围定位问题。</small></div><ArrowRight aria-hidden="true" /></Link>
          </div>
        </section>
      </div>
    </section>
  );
}
