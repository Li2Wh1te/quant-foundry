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
  request_completed: { title: "请求完成" },
  request_failed: { title: "请求失败" },
  task_run_started: { title: "任务开始", summary: "调度器已开始执行该任务。" },
  task_run_succeeded: { title: "任务成功", summary: "任务已执行完成。" },
  task_run_failed: { title: "任务失败", summary: "任务执行失败，请查看异常详情。" },
  trade_calendar_sync_planned: { title: "生成采集计划" },
  trade_calendar_range_started: { title: "开始采集分段" },
  trade_calendar_range_succeeded: { title: "完成采集分段" },
  trade_calendar_range_failed: { title: "采集分段失败" },
  trade_calendar_sync_completed: { title: "交易日历采集完成" },
  etf_daily_incremental_sync_planned: { title: "生成ETF日线增量采集计划" },
  etf_daily_incremental_sync_started: { title: "开始采集ETF日线增量" },
  etf_daily_incremental_sync_succeeded: { title: "完成采集ETF日线增量" },
  etf_daily_incremental_sync_failed: { title: "ETF日线增量采集失败" },
  etf_daily_incremental_sync_completed: { title: "ETF日线增量采集完成" },
  etf_daily_full_sync_planned: { title: "生成ETF日线全量采集计划" },
  etf_daily_full_sync_started: { title: "开始采集ETF日线全量" },
  etf_daily_full_sync_succeeded: { title: "完成采集ETF日线全量" },
  etf_daily_full_sync_failed: { title: "ETF日线全量采集失败" },
  etf_daily_full_sync_completed: { title: "ETF日线全量采集完成" }
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
  const parts: string[] = [];
  if (entry.method) parts.push(entry.method);
  if (entry.path) parts.push(entry.path);
  if (typeof entry.status_code === "number") parts.push(String(entry.status_code));
  if (typeof entry.duration_ms === "number") parts.push(`${entry.duration_ms} ms`);
  if (parts.length > 0) return parts.join("  ·  ");
  const message = entry.message;
  return typeof message === "string" ? message : eventPresentation(entry).summary ?? "系统记录了一条运行事件。";
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
          <span>时间 / 级别</span><span>事件</span><span>内容</span><span>Request ID</span>
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
                  <strong>{entryTitle(entry)}</strong>
                  <code className="log-entry__summary">{describeEntry(entry)}</code>
                  <code className="log-entry__request">{entry.request_id || "-"}</code>
                  <ChevronDown aria-hidden="true" />
                </summary>
                <pre>{JSON.stringify(entry, null, 2)}</pre>
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
