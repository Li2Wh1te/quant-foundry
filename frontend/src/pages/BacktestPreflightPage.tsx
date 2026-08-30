import { useCallback, useEffect, useState } from "react";

import {
  BacktestPreflightError,
  BacktestPreflightItem,
  fetchBacktestPreflight,
  PreflightSection,
  compareDataRevisionFields
} from "../api/backtestPreflight";
import { useAuth } from "../auth/AuthContext";
import "../backtestPreflight.css";

function jsonText(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

function reportTitle(item: BacktestPreflightItem): string {
  return item.title ?? (item.status === "ready" ? "数据预检已通过" : "数据预检未通过");
}

function revisionSummary(item: BacktestPreflightItem): Record<string, any> | null {
  return item.data_revision_summary && typeof item.data_revision_summary === "object" ? item.data_revision_summary : null;
}

/**
 * Minimal run-detail surface for the canonical data-preflight resource.
 * It deliberately renders machine evidence and the server-provided Chinese
 * issue text; it does not infer a calendar, retry a blocked run, or expose a
 * legacy endpoint.
 */
export function BacktestPreflightPage() {
  const { logout } = useAuth();
  const [runId, setRunId] = useState("");
  const [activeRunId, setActiveRunId] = useState("");
  const [section, setSection] = useState<PreflightSection | undefined>();
  const [page, setPage] = useState<{ items: BacktestPreflightItem[]; next_cursor: string | null; has_more: boolean; truncated: boolean } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [compareRunId, setCompareRunId] = useState("");
  const [comparePage, setComparePage] = useState<typeof page>(null);

  const load = useCallback(async (nextRunId: string, nextSection?: PreflightSection, cursor?: string) => {
    if (!nextRunId.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const result = await fetchBacktestPreflight(nextRunId.trim(), { section: nextSection, limit: nextSection ? 100 : 100, cursor });
      setActiveRunId(nextRunId.trim());
      setSection(nextSection);
      setPage(result);
    } catch (caught) {
      if (caught instanceof BacktestPreflightError && caught.status === 401) {
        logout();
        return;
      }
      setError(caught instanceof Error ? caught.message : "预检详情加载失败。");
    } finally {
      setLoading(false);
    }
  }, [logout]);

  const compare = async () => {
    if (!compareRunId.trim()) return;
    try {
      const result = await fetchBacktestPreflight(compareRunId.trim(), { limit: 100 });
      setComparePage(result);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "对比运行加载失败。");
    }
  };

  useEffect(() => () => undefined, []);

  return (
    <main className="page-content" aria-labelledby="backtest-preflight-title">
      <header className="page-header">
        <div>
          <p className="eyebrow">回测运行详情</p>
          <h1 id="backtest-preflight-title">交易日历与会话预检</h1>
          <p className="page-subtitle">仅展示已创建运行的冻结预检证据；阻断创建不会生成可查询运行。</p>
        </div>
      </header>

      <section className="panel" aria-label="查询回测预检">
        <form className="form-grid" onSubmit={(event) => { event.preventDefault(); void load(runId, section); }}>
          <label>
            运行 ID
            <input value={runId} onChange={(event) => setRunId(event.target.value)} placeholder="输入 UUID" />
          </label>
          <label>
            详情区段
            <select value={section ?? ""} onChange={(event) => setSection((event.target.value || undefined) as PreflightSection | undefined)}>
              <option value="">报告列表</option>
              <option value="calendar">日历详情</option>
              <option value="sessions">会话详情</option>
            </select>
          </label>
          <button className="button button--primary" type="submit" disabled={loading || !runId.trim()}>
            {loading ? "加载中…" : "查询预检"}
          </button>
        </form>
        <div className="compare-row">
          <label>第二运行 ID<input value={compareRunId} onChange={(event) => setCompareRunId(event.target.value)} placeholder="可选：输入 UUID 进行摘要对比" /></label>
          <button className="button button--secondary" type="button" onClick={() => void compare()} disabled={!compareRunId.trim() || loading}>对比修订摘要</button>
        </div>
      </section>

      {error && <div className="alert alert--error" role="alert">{error}</div>}
      {page && (
        <section className="panel" aria-live="polite">
          <div className="panel-header">
            <div>
              <p className="eyebrow">{activeRunId}</p>
              <h2>{section ? `${section === "calendar" ? "日历" : "会话"}详情` : "预检报告"}</h2>
            </div>
            <span className={page.items.some((item) => item.status !== "ready") ? "status-badge status-badge--warning" : "status-badge status-badge--success"}>
              {page.items.some((item) => item.status !== "ready") ? "存在阻断或降级" : "已通过"}
            </span>
          </div>
          {page.items.length === 0 && <p className="empty-state">没有可查询的预检报告。</p>}
          {page.items.map((item) => (
            <article className="preflight-card" key={`${item.phase}-${item.report_hash}`}>
              <div className="preflight-card__heading">
                <h3>{reportTitle(item)}</h3>
                <code>{item.phase} · hash schema {item.hash_schema_version}</code>
              </div>
              <p className="page-subtitle">{item.message ?? "预检结果。"}</p>
              <dl className="preflight-meta">
                <div><dt>运行类型</dt><dd>{item.run_kind === "internal_link_acceptance" ? "内部链路验收" : "正式回测"} · {item.preflight_profile ?? "formal@1"}</dd></div>
                <div><dt>report_hash</dt><dd><code>{item.report_hash}</code></dd></div>
                {(item.admission_report_hash || item.session_report_hash) && <div><dt>页面 / 会话 hash</dt><dd><code>{item.admission_report_hash ?? "未提供"}</code> / <code>{item.session_report_hash ?? "未提供"}</code>{item.hash_match === false ? "（不一致，已阻断）" : item.hash_match === true ? "（匹配）" : ""}</dd></div>}
                <div><dt>PIT 状态</dt><dd>{item.pit_status ?? (item.pit_profile ?? "未提供")}</dd></div>
                <div><dt>data_cutoff</dt><dd><code>{item.data_cutoff ?? "未提供"}</code></dd></div>
                <div><dt>日历修订摘要</dt><dd><code>{item.calendar_revision_digest ?? "未提供"}</code></dd></div>
                <div><dt>snapshot fingerprint</dt><dd><code>{item.snapshot_fingerprint ?? "未提供"}</code></dd></div>
                <div><dt>non-strict PIT</dt><dd>{item.non_strict_pit == null ? "未提供" : item.non_strict_pit ? "是" : "否"}</dd></div>
                <div><dt>non-strict 能力</dt><dd>{item.non_strict_pit_capabilities?.join("、") ?? "未提供"}</dd></div>
              </dl>
              {item.calendar_summary && <details open={section === "calendar"}><summary>日历与覆盖证据</summary><pre>{jsonText(item.calendar_summary)}</pre></details>}
              {item.session_summary && <details open={section === "sessions"}><summary>formal / warmup 会话证据</summary><pre>{jsonText(item.session_summary)}</pre></details>}
              {item.coverage && <details><summary>数据覆盖</summary><pre>{jsonText(item.coverage)}</pre></details>}
              {item.source_revisions && <details><summary>来源修订</summary><pre>{jsonText(item.source_revisions)}</pre></details>}
              <details><summary>源数据修订摘要</summary>{revisionSummary(item) ? <>
                <dl className="preflight-meta revision-meta">
                  <div><dt>审计资格</dt><dd>{revisionSummary(item)?.qualification?.eligible ? "可用" : "不可用"}</dd></div>
                  <div><dt>证据类别</dt><dd>{revisionSummary(item)?.qualification?.evidence_class ?? "未提供"}</dd></div>
                  <div><dt>来源版本</dt><dd><code>{revisionSummary(item)?.contract ?? "未提供"}</code></dd></div>
                  <div><dt>修订 hash</dt><dd><code>{revisionSummary(item)?.revision_vector_hash ?? "未提供"}</code></dd></div>
                  <div><dt>有效时间范围</dt><dd>{jsonText(revisionSummary(item)?.capabilities?.bars?.valid_time_range ?? "未提供")}</dd></div>
                  <div><dt>影响范围 / correction 计数</dt><dd>{jsonText(revisionSummary(item)?.capabilities?.bars?.affected_range ?? "未提供")}</dd></div>
                </dl><pre>{jsonText(item.data_revision_summary)}</pre>
              </> : <p className="muted">未提供</p>}</details>
            </article>
          ))}
          {page.has_more && page.next_cursor && (
            <button className="button button--secondary" type="button" onClick={() => void load(activeRunId, section, page.next_cursor ?? undefined)} disabled={loading}>
              下一页
            </button>
          )}
          {page.truncated && <p className="muted">当前结果仍有分页内容，请继续加载。</p>}
          {comparePage?.items[0] && page.items[0] && <details className="compare-panel" open><summary>双运行功能字段对比</summary><pre>{jsonText(compareDataRevisionFields(page.items[0], comparePage.items[0]))}</pre></details>}
        </section>
      )}
    </main>
  );
}
