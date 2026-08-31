import {
  ChevronDown,
  Download,
  Filter,
  RefreshCw,
  Search,
  Trash2,
  X
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { useNavigate } from "react-router-dom";

import {
  clearLogs,
  HttpMethod,
  LogEntry,
  LogLevel,
  LogSearchFilters,
  LogSearchResult,
  searchLogs,
  StatusClass,
  UnauthorizedError
} from "../api/logs";
import { useAuth } from "../auth/AuthContext";

const METHODS: HttpMethod[] = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"];
const LEVELS: LogLevel[] = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];
const STATUS_CLASSES: StatusClass[] = ["2xx", "3xx", "4xx", "5xx"];

const EMPTY_RESULT: LogSearchResult = {
  items: [],
  matched_count: 0,
  truncated: false,
  scanned_files: 0,
  facets: { levels: {}, methods: {}, status_classes: {}, paths: {} }
};

function initialFilters(): LogSearchFilters {
  return {
    startTime: new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString(),
    limit: 200
  };
}

function formatTime(timestamp: string | undefined): string {
  if (!timestamp) return "时间未知";
  const value = new Date(timestamp);
  if (Number.isNaN(value.getTime())) return timestamp;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false
  }).format(value);
}

function levelClass(level: string | undefined): string {
  return `log-level log-level--${(level ?? "info").toLowerCase()}`;
}

interface EventPresentation {
  title: string;
  summary?: string;
}

const EVENT_PRESENTATIONS: Record<string, EventPresentation> = {
  request_completed: { title: "请求完成", summary: "接口请求已完成。" },
  request_failed: { title: "请求失败", summary: "接口请求失败，请查看展开详情。" },
  task_run_started: { title: "任务开始", summary: "调度器已开始执行该任务。" },
  task_run_succeeded: { title: "任务成功", summary: "任务已执行完成。" },
  task_run_failed: { title: "任务失败", summary: "任务执行失败，请查看异常详情。" },
  task_run_worker_crashed: { title: "任务执行器异常退出", summary: "任务执行器异常退出，系统正在补偿结束运行记录。" },
  task_run_failure_recovery_failed: { title: "任务运行记录补偿失败", summary: "任务执行器异常后的运行记录补偿失败，请查看异常详情。" },
  task_run_cancel_requested: { title: "请求取消任务", summary: "Supervisor 已请求取消任务运行，正在等待执行器退出。" },
  task_run_cancelled: { title: "任务已取消", summary: "任务运行已取消，终态记录已写入。" },
  task_run_worker_exited: { title: "任务子进程退出", summary: "任务子进程已退出，系统正在核对退出码并写入运行终态。" },
  task_run_terminal_written: { title: "任务终态已写入", summary: "任务运行终态已写入，完成标记和退出信息可在详情中查看。" },
  task_run_lost_heartbeat_terminated: { title: "任务心跳失联并终止", summary: "任务运行连续 60 秒无有效心跳，已在取消宽限期后终止，原运行不会重启或复用。" },
  runner_supervisor_lock_acquired: { title: "回测 Supervisor 取得锁", summary: "回测 Supervisor 已取得单实例锁并开始负责队列。" },
  runner_supervisor_lock_busy: { title: "回测 Supervisor 未取得锁", summary: "已有回测 Supervisor 正在运行，本实例未领取任务。" },
  runner_supervisor_recovery: { title: "回测 Supervisor 完成恢复扫描", summary: "回测 Supervisor 已扫描遗留运行并保留恢复审计证据。" },
  backtest_worker_started: { title: "回测 worker 已启动", summary: "回测运行已领取并启动 worker，正在等待身份握手。" },
  backtest_worker_handshake: { title: "回测 worker 握手完成", summary: "回测 worker 身份握手已验证，运行进入执行状态。" },
  backtest_worker_launch_failed: { title: "回测 worker 启动失败", summary: "回测 worker 启动失败，运行已进入不确定终态。" },
  backtest_worker_termination_requested: { title: "请求终止回测 worker", summary: "回测 worker 已收到终止请求，Supervisor 正在等待退出。" },
  backtest_runner_lost_heartbeat: { title: "回测心跳失联", summary: "回测运行连续 60 秒无有效心跳，已进入终止复核且不会自动重启。" },
  backtest_run_terminal: { title: "回测运行进入终态", summary: "回测运行已完成退出码、完成标记和完整性证据复核。" },
  backtest_terminal_written: { title: "回测终态已写入", summary: "回测运行终态及原因已写入结构化审计记录。" },
  scheduler_started: { title: "调度器启动", summary: "调度器已启动并加载活动任务。" },
  scheduler_stopped: { title: "调度器停止", summary: "调度器已停止运行。" },
  scheduler_disabled: { title: "调度器未启用", summary: "调度器配置为未启用，未执行调度任务。" },
  task_sync_failed: { title: "任务同步失败", summary: "任务配置同步失败，请查看展开详情。" },
  scheduled_run_ignored: { title: "计划运行已忽略", summary: "该计划运行与并发策略冲突，已按规则忽略。" },
  scheduled_run_enqueue_failed: { title: "计划运行入队失败", summary: "计划运行未能加入执行队列，请查看展开详情。" },
  application_started: { title: "应用启动", summary: "应用服务已启动。" },
  application_stopped: { title: "应用停止", summary: "应用服务已停止。" },
  trade_calendar_sync_completed: { title: "交易日历采集完成" },
  trade_calendar_sync_planned: { title: "生成交易日历采集计划", summary: "已生成交易日历采集分段计划。" },
  trade_calendar_range_started: { title: "开始采集交易日历", summary: "正在采集交易日历分段。" },
  trade_calendar_range_succeeded: { title: "完成交易日历采集", summary: "交易日历分段已完成，详情包含日期范围和计数。" },
  trade_calendar_range_failed: { title: "交易日历采集失败", summary: "交易日历分段采集失败，checkpoint 未推进。" },
  trade_calendar_named_sync_planned: { title: "生成具名交易日历采集计划" },
  trade_calendar_named_range_started: { title: "开始采集具名日历", summary: "正在采集具名交易日历分段。" },
  trade_calendar_named_range_succeeded: { title: "完成具名日历采集", summary: "具名交易日历分段已完成，详情包含范围、计数和 checkpoint。" },
  trade_calendar_named_range_failed: { title: "具名日历采集失败", summary: "具名交易日历分段采集失败，checkpoint 未推进。" },
  calendar_reconciliation_started: { title: "开始日历修订校验", summary: "正在校验日历修订范围和覆盖缺口。" },
  calendar_reconciliation_completed: { title: "完成日历修订校验", summary: "日历修订范围已完成校验并更新覆盖证据。" },
  calendar_reconciliation_blocked: { title: "日历修订校验阻断", summary: "日历修订范围仍有缺口或冲突，checkpoint 未推进。" },
  corporate_action_calendar_unresolved: { title: "公司行动日历未解析", summary: "公司行动关联的交易日历暂未解析，请查看展开详情。" },
  legacy_trade_calendar_backfill_completed: { title: "历史交易日历回填完成", summary: "历史交易日历回填已完成。" },
  etf_basic_sync_started: {
    title: "开始采集 ETF 基础信息",
    summary: "正在拉取全部状态的 ETF 基础信息。"
  },
  etf_basic_sync_succeeded: {
    title: "完成采集 ETF 基础信息",
    summary: "ETF 基础信息已成功写入数据库并推进检查点。"
  },
  etf_basic_sync_failed: {
    title: "ETF 基础信息采集失败",
    summary: "ETF 基础信息未完成采集，请查看异常详情。"
  },
  etf_basic_sync_completed: {
    title: "ETF 基础信息采集完成",
    summary: "ETF 基础信息采集任务已执行完成。"
  },
  etf_daily_incremental_sync_planned: { title: "生成 ETF 日线增量采集计划" },
  etf_daily_incremental_sync_started: { title: "开始采集 ETF 日线增量" },
  etf_daily_incremental_sync_succeeded: { title: "完成采集 ETF 日线增量" },
  etf_daily_incremental_sync_failed: { title: "ETF 日线增量采集失败" },
  etf_daily_incremental_sync_completed: { title: "ETF 日线增量采集完成" },
  etf_daily_full_sync_planned: { title: "生成 ETF 日线全量采集计划" },
  etf_daily_full_sync_started: { title: "开始采集 ETF 日线全量" },
  etf_daily_full_sync_succeeded: { title: "完成采集 ETF 日线全量" },
  etf_daily_full_sync_failed: { title: "ETF 日线全量采集失败" },
  etf_daily_full_sync_completed: { title: "ETF 日线全量采集完成" },
  etf_daily_calendar_succeeded: { title: "完成按日历采集 ETF 日线" },
  etf_daily_calendar_planned: { title: "生成按日历采集 ETF 日线计划" },
  etf_daily_calendar_started: { title: "开始按日历采集 ETF 日线" },
  etf_daily_calendar_failed: { title: "按日历采集 ETF 日线失败" },
  etf_cash_dividend_incremental_sync_started: { title: "开始采集 ETF 现金分红增量", summary: "正在拉取公告日期范围内的 ETF 现金分红。" },
  etf_cash_dividend_incremental_sync_succeeded: { title: "完成采集 ETF 现金分红增量" },
  etf_cash_dividend_incremental_sync_failed: { title: "ETF 现金分红增量采集失败", summary: "现金分红增量采集失败，checkpoint 未推进。" },
  etf_cash_dividend_full_sync_started: { title: "开始采集 ETF 现金分红全量" },
  etf_cash_dividend_full_sync_succeeded: { title: "完成采集 ETF 现金分红全量" },
  etf_cash_dividend_reconciliation_started: { title: "开始校验 ETF 现金分红" },
  etf_cash_dividend_reconciliation_succeeded: { title: "完成校验 ETF 现金分红" },
  etf_adjustment_incremental_sync_planned: { title: "生成 ETF 复权因子增量计划" },
  etf_adjustment_incremental_sync_started: { title: "开始采集 ETF 复权因子增量" },
  etf_adjustment_incremental_sync_succeeded: { title: "完成采集 ETF 复权因子增量" },
  etf_adjustment_incremental_sync_failed: { title: "ETF 复权因子增量采集失败" },
  etf_adjustment_full_sync_planned: { title: "生成 ETF 复权因子全量计划" },
  etf_adjustment_full_sync_started: { title: "开始采集 ETF 复权因子全量" },
  etf_adjustment_full_sync_succeeded: { title: "完成采集 ETF 复权因子全量" },
  etf_adjustment_full_sync_failed: { title: "ETF 复权因子全量采集失败" },
  etf_adjustment_reconciliation_planned: { title: "生成 ETF 复权因子校验计划" },
  etf_adjustment_reconciliation_started: { title: "开始校验 ETF 复权因子" },
  etf_adjustment_reconciliation_succeeded: { title: "完成校验 ETF 复权因子" },
  etf_adjustment_reconciliation_failed: { title: "ETF 复权因子校验失败" },
  etf_adjustment_sync_started: { title: "开始采集 ETF 复权因子" },
  etf_adjustment_sync_succeeded: { title: "完成采集 ETF 复权因子" },
  etf_adjustment_sync_failed: { title: "ETF 复权因子采集失败" },
  etf_adjustment_calendar_succeeded: { title: "完成按日历采集 ETF 复权因子" },
  etf_adjustment_calendar_planned: { title: "生成按日历采集 ETF 复权因子计划" },
  etf_adjustment_incremental_planned: { title: "生成按日历采集 ETF 复权因子计划" },
  etf_adjustment_incremental_started: { title: "开始按日历采集 ETF 复权因子" },
  etf_adjustment_incremental_failed: { title: "按日历采集 ETF 复权因子失败" },
  etf_adjustment_full_planned: { title: "生成按日历采集 ETF 复权因子计划" },
  etf_adjustment_full_started: { title: "开始按日历采集 ETF 复权因子" },
  etf_adjustment_full_failed: { title: "按日历采集 ETF 复权因子失败" }
};

function eventPresentation(entry: LogEntry): EventPresentation {
  const event = entry.event;
  if (typeof event !== "string" || !event) return { title: "系统运行事件", summary: "系统记录了一条运行事件。" };
  const known = EVENT_PRESENTATIONS[event];
  if (known) return known;

  // APScheduler emits plain English messages through Python logging. Translate
  // its recurring dispatcher events so the log list stays readable to operators.
  if (event.includes("dispatch_queued_runs")) {
    if (event.startsWith("Running job")) return { title: "开始派发任务", summary: "调度器正在检查等待执行的任务。" };
    if (event.includes("executed successfully")) return { title: "任务派发完成", summary: "调度器已完成本次等待任务检查。" };
    return { title: "调度任务派发", summary: "调度器正在处理等待执行的任务。" };
  }
  if (event.startsWith("Running job")) return { title: "开始执行调度任务", summary: "调度器正在执行一项内部调度操作。" };
  if (event.includes("executed successfully")) return { title: "调度任务完成", summary: "调度器已成功完成一项内部调度操作。" };
  return { title: "系统运行事件", summary: "系统记录了一条运行事件。" };
}

function entryTitle(entry: LogEntry): string {
  return eventPresentation(entry).title;
}

function describeEntry(entry: LogEntry): string {
  const message = typeof entry.message === "string" ? entry.message.trim() : "";
  if (message && /[\u3400-\u9fff]/.test(message)) return message;

  const presentation = eventPresentation(entry);
  if (presentation.summary) return presentation.summary;

  // Known events without a backend message still receive a concise Chinese
  // fallback rather than exposing an English logger payload as the summary.
  if (typeof entry.event === "string" && EVENT_PRESENTATIONS[entry.event]) {
    return `${presentation.title}。`;
  }

  // Keep operator-facing summaries Chinese even when a web server or a third
  // party emits only technical English. Raw fields remain available on expand.
  const scope = entry.path ? `接口 ${entry.path}` : "接口请求";
  const outcome = typeof entry.status_code === "number" ? `返回 ${entry.status_code}` : "已记录运行结果";
  const duration = typeof entry.duration_ms === "number" ? `，耗时 ${entry.duration_ms} ms` : "";
  return `${scope}${outcome}${duration}。`;
}

function FilterChip({
  active,
  count,
  children,
  onClick
}: {
  active: boolean;
  count: number;
  children: React.ReactNode;
  onClick: () => void;
}) {
  return (
    <button
      className={`filter-chip${active ? " filter-chip--active" : ""}`}
      type="button"
      aria-pressed={active}
      onClick={onClick}
    >
      {children}<span>{count}</span>
    </button>
  );
}

export function LogPage() {
  const { logout } = useAuth();
  const navigate = useNavigate();
  const [draftKeyword, setDraftKeyword] = useState("");
  const [draftPath, setDraftPath] = useState("");
  const [filters, setFilters] = useState<LogSearchFilters>(initialFilters);
  const [timeRange, setTimeRange] = useState<"24h" | "30d">("24h");
  const [result, setResult] = useState<LogSearchResult>(EMPTY_RESULT);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filtersOpen, setFiltersOpen] = useState(true);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const requestId = useRef(0);

  const runQuery = useCallback(async (nextFilters: LogSearchFilters, background = false) => {
    const currentRequest = ++requestId.current;
    if (background) setRefreshing(true);
    else setLoading(true);
    setError(null);
    try {
      const nextResult = await searchLogs(nextFilters);
      if (currentRequest === requestId.current) setResult(nextResult);
    } catch (caught) {
      if (currentRequest !== requestId.current) return;
      if (caught instanceof UnauthorizedError) {
        logout();
        navigate("/login", { replace: true });
        return;
      }
      setError(caught instanceof Error ? caught.message : "日志加载失败。");
    } finally {
      if (currentRequest === requestId.current) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, [logout, navigate]);

  useEffect(() => {
    void runQuery(filters);
  }, [filters, runQuery]);

  useEffect(() => {
    if (!autoRefresh) return;
    const timer = window.setInterval(() => void runQuery(filters, true), 10_000);
    return () => window.clearInterval(timer);
  }, [autoRefresh, filters, runQuery]);

  const activeFilterCount = useMemo(() => [
    filters.level,
    filters.method,
    filters.statusClass,
    filters.path
  ].filter(Boolean).length, [filters]);

  function submitSearch(event: FormEvent) {
    event.preventDefault();
    setFilters((current) => ({
      ...current,
      keyword: draftKeyword.trim() || undefined,
      path: draftPath.trim() || undefined
    }));
  }

  function toggleFilter<K extends "level" | "method" | "statusClass">(
    key: K,
    value: NonNullable<LogSearchFilters[K]>
  ) {
    setFilters((current) => ({
      ...current,
      [key]: current[key] === value ? undefined : value
    }));
  }

  function clearFilters() {
    setDraftKeyword("");
    setDraftPath("");
    setTimeRange("24h");
    setFilters(initialFilters());
  }

  async function handleClear() {
    if (!window.confirm("确认清空当前可见日志？此操作不会删除物理日志文件。")) return;
    setClearing(true);
    setError(null);
    try {
      await clearLogs();
      await runQuery(filters);
    } catch (caught) {
      if (caught instanceof UnauthorizedError) {
        logout();
        navigate("/login", { replace: true });
      } else {
        setError(caught instanceof Error ? caught.message : "清空日志失败。");
      }
    } finally {
      setClearing(false);
    }
  }

  function downloadResults() {
    const content = result.items.map((entry) => JSON.stringify(entry)).join("\n");
    const blob = new Blob([content ? `${content}\n` : ""], { type: "application/x-ndjson" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `quant-foundry-logs-${new Date().toISOString().replace(/[:.]/g, "-")}.jsonl`;
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <section className="logs-page" aria-labelledby="logs-title">
      <div className="page-heading">
        <div>
          <h2 id="logs-title">日志查询</h2>
          <p>检索服务运行事件与 API 请求记录</p>
        </div>
        <div className="page-heading__meta">
          <span>{result.scanned_files} 个日志文件</span>
          <span>{result.matched_count.toLocaleString("zh-CN")} 条匹配</span>
        </div>
      </div>

      <form className="log-query" onSubmit={submitSearch}>
        <div className="log-query__primary">
          <label className="search-field">
            <Search aria-hidden="true" />
            <span className="sr-only">搜索日志</span>
            <input
              value={draftKeyword}
              onChange={(event) => setDraftKeyword(event.target.value)}
              placeholder="搜索事件、Request ID 或日志内容"
              maxLength={200}
            />
          </label>
          <button className="query-button" type="submit">查询</button>
          <button
            className={`toolbar-button${filtersOpen ? " toolbar-button--active" : ""}`}
            type="button"
            onClick={() => setFiltersOpen((open) => !open)}
            aria-expanded={filtersOpen}
          >
            <Filter aria-hidden="true" />
            筛选{activeFilterCount > 0 && <span className="count-badge">{activeFilterCount}</span>}
            <ChevronDown className={filtersOpen ? "chevron--up" : ""} aria-hidden="true" />
          </button>
        </div>

        {filtersOpen && (
          <div className="structured-filters">
            <div className="filter-row">
              <span className="filter-row__label">级别</span>
              <div className="filter-row__options">
                {LEVELS.map((level) => (
                  <FilterChip
                    key={level}
                    active={filters.level === level}
                    count={result.facets.levels[level] ?? 0}
                    onClick={() => toggleFilter("level", level)}
                  >{level}</FilterChip>
                ))}
              </div>
            </div>
            <div className="filter-row">
              <span className="filter-row__label">方法</span>
              <div className="filter-row__options">
                {METHODS.map((method) => (
                  <FilterChip
                    key={method}
                    active={filters.method === method}
                    count={result.facets.methods[method] ?? 0}
                    onClick={() => toggleFilter("method", method)}
                  >{method}</FilterChip>
                ))}
              </div>
            </div>
            <div className="filter-row">
              <span className="filter-row__label">状态</span>
              <div className="filter-row__options">
                {STATUS_CLASSES.map((statusClass) => (
                  <FilterChip
                    key={statusClass}
                    active={filters.statusClass === statusClass}
                    count={result.facets.status_classes[statusClass] ?? 0}
                    onClick={() => toggleFilter("statusClass", statusClass)}
                  >{statusClass}</FilterChip>
                ))}
              </div>
            </div>
            <div className="filter-row filter-row--fields">
              <label>
                <span>精确路径</span>
                <input
                  value={draftPath}
                  onChange={(event) => setDraftPath(event.target.value)}
                  placeholder="/api/orders"
                  maxLength={500}
                />
              </label>
              <label>
                <span>时间范围</span>
                <select
                  value={timeRange}
                  onChange={(event) => {
                    const nextRange = event.target.value as "24h" | "30d";
                    setTimeRange(nextRange);
                    const duration = nextRange === "24h" ? 24 : 30 * 24;
                    setFilters((current) => ({
                      ...current,
                      startTime: new Date(Date.now() - duration * 60 * 60 * 1000).toISOString()
                    }));
                  }}
                >
                  <option value="24h">最近 24 小时</option>
                  <option value="30d">最近 30 天</option>
                </select>
              </label>
              <button className="clear-filter-button" type="button" onClick={clearFilters}>
                <X aria-hidden="true" />清除条件
              </button>
            </div>
          </div>
        )}
      </form>

      <div className="log-toolbar">
        <div className="auto-refresh-control">
          <button
            className={`switch${autoRefresh ? " switch--on" : ""}`}
            type="button"
            role="switch"
            aria-checked={autoRefresh}
            onClick={() => setAutoRefresh((enabled) => !enabled)}
          ><span /></button>
          <span>每 10 秒自动刷新</span>
        </div>
        <div className="log-toolbar__actions">
          <button className="toolbar-button" type="button" onClick={() => void runQuery(filters, true)} disabled={refreshing}>
            <RefreshCw className={refreshing ? "spin" : ""} aria-hidden="true" />刷新
          </button>
          <button className="toolbar-button" type="button" onClick={downloadResults} disabled={result.items.length === 0}>
            <Download aria-hidden="true" />导出当前结果
          </button>
          <button className="toolbar-button toolbar-button--danger" type="button" onClick={() => void handleClear()} disabled={clearing}>
            <Trash2 aria-hidden="true" />{clearing ? "清空中" : "清空日志"}
          </button>
        </div>
      </div>

      {error && <div className="log-message log-message--error" role="alert">{error}</div>}

      <div className="log-results" aria-busy={loading}>
        <div className="log-results__header">
          <span>时间 / 级别</span><span>事件与结果摘要</span>
        </div>
        {loading ? (
          <div className="log-message"><RefreshCw className="spin" aria-hidden="true" />正在读取日志...</div>
        ) : result.items.length === 0 ? (
          <div className="log-empty">
            <Search aria-hidden="true" />
            <strong>没有找到匹配日志</strong>
            <span>调整关键词、筛选条件或时间范围后重试。</span>
          </div>
        ) : (
          <div className="log-list">
            {result.items.map((entry, index) => (
              <details className="log-entry" key={`${entry.timestamp ?? "entry"}-${entry.request_id ?? index}-${index}`}>
                <summary>
                  <div className="log-entry__time">
                    <time dateTime={entry.timestamp}>{formatTime(entry.timestamp)}</time>
                    <span className={levelClass(entry.level)}>{(entry.level ?? "INFO").toUpperCase()}</span>
                  </div>
                  <div className="log-entry__event">
                    <strong>{entryTitle(entry)}</strong>
                    <span className="log-entry__summary">{describeEntry(entry)}</span>
                  </div>
                  <ChevronDown aria-hidden="true" />
                </summary>
                <div className="log-entry__detail">
                  <dl>
                    <div><dt>Request ID</dt><dd>{entry.request_id || "—"}</dd></div>
                    <div><dt>任务 ID</dt><dd>{entry.task_id || "—"}</dd></div>
                    <div><dt>运行 ID</dt><dd>{entry.run_id || "—"}</dd></div>
                    <div><dt>任务类型</dt><dd>{entry.task_type || "—"}</dd></div>
                    <div><dt>错误类型</dt><dd>{entry.error_type || "—"}</dd></div>
                    <div><dt>错误信息</dt><dd>{entry.error_message || "—"}</dd></div>
                    <div><dt>接口路径</dt><dd>{entry.path || "—"}</dd></div>
                    <div><dt>HTTP 状态</dt><dd>{entry.status_code ?? "—"}</dd></div>
                    <div><dt>耗时</dt><dd>{typeof entry.duration_ms === "number" ? `${entry.duration_ms} ms` : "—"}</dd></div>
                  </dl>
                  {(entry.stack || entry.exception) && <pre className="log-entry__stack">{entry.stack || entry.exception}</pre>}
                  <pre>{JSON.stringify(entry, null, 2)}</pre>
                </div>
              </details>
            ))}
          </div>
        )}
        {!loading && result.truncated && (
          <div className="log-results__footnote">当前显示最新 {result.items.length} 条，共匹配 {result.matched_count.toLocaleString("zh-CN")} 条。</div>
        )}
      </div>
    </section>
  );
}
