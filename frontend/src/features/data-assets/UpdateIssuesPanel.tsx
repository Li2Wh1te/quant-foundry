import { CircleAlert, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { currentStatus, DataStoreApiError, formatCount as count, formatTimestamp as time,
  isCancellation, issueReason, updateStatus, type CurrentDataset, type CurrentIssue } from "./data";
import { DataSheet, LocalNotice } from "./components";
import { useDataAssetsFailure } from "./components/useDataAssetsFailure";
import { viewParams } from "./navigation";
import { createUpdateIssuesSession, type IssuesSnapshot } from "./UpdateIssuesSession";
import "./UpdateIssuesPanel.css";

export interface UpdateIssuesPanelProps { dataset: CurrentDataset; refreshVersion: number; }

/** Local explanations do not change D01's shared dictionary or infer server permission. */
export function issueExplanation(reason: unknown) {
  const base = issueReason(reason);
  const explanations: Record<string, [string, string]> = {
    SOURCE_REFRESH_FAILED: ["本次来源刷新失败", "查看已有采集任务的结果，再刷新状态。旧数据是否可读仍以当前状态和实际查询为准。"],
    FULL_RANGE_UNPROVEN: ["完整范围的确认依据不足", "调整到明确的对象与范围查看已有数据。这不表示全部数值错误，也不证明完整覆盖。"],
    SOURCE_CONFIRMATION_UNPROVEN: [base.label, "查看已有数据源说明和确认依据；实际读取范围由服务端判断。"],
    REPORT_INCOMPLETE: [base.label, "调整报告对象或范围查看已有数据；单页成员不代表完整报告。"],
    SOURCE_RANGES_BLOCKED: [base.label, "查看已有采集任务记录，确认尚未处理的范围后再刷新状态。"],
    NATIVE_RANGES_PENDING: ["部分来源范围尚未处理", "查看已有采集任务记录，确认处理范围后再刷新状态。"],
    LOCAL_SCAN_INCOMPLETE: ["本地范围扫描未完成", "查看已有采集任务的结果；当前覆盖仍待确认。"],
    LEGACY_RESTRICTION: [base.label, "查看限制范围和已有数据源说明；当前查询仍遵守服务端限制。"],
    CURRENT_SOURCE_MISMATCH: [base.label, "查看已有数据源说明，确认当前来源与对象后再读取。"],
    ISSUE_BUDGET_EXCEEDED: [base.label, "查看已有任务的处理结果和范围；本页仅刷新已登记的问题。"],
    LOCAL_UPDATE_BUDGET_EXCEEDED: [base.label, "查看已有采集任务记录，确认本次已处理的范围。"],
    DATA_RESTRICTED: [base.label, "调整当前对象或范围；读取权限仍由服务端判断。"],
    DATA_CHANGED: [base.label, "刷新当前状态，再重新选择对象与读取范围。"],
    REBUILD_REQUIRED: [base.label, "查看已有任务记录并等待服务端处理，再刷新状态。"]
  };
  const detail = base.diagnosticCode ? explanations[base.diagnosticCode] : undefined;
  return { label: detail?.[0] ?? base.label,
    next: detail?.[1] ?? "服务端尚未提供可解释的原因。可展开诊断信息定位，或查看已有任务记录。",
    code: base.diagnosticCode };
}

export function UpdateSummary({ dataset }: { dataset: CurrentDataset }) {
  const update = dataset.last_update, explanation = update?.reason ? issueExplanation(update.reason) : null;
  return <>
    <DataSheet title="当前数据状态">
      <dl className="qf-assets-properties">
        <div><dt>当前状态</dt><dd>{currentStatus(dataset.status).label}</dd></div>
        <div><dt>当前读取权限</dt><dd>以服务端对实际对象与范围的查询判断为准。</dd></div>
        <div><dt>业务日期覆盖</dt><dd>未声明</dd></div>
        <div><dt>业务数据截至日</dt><dd>未声明</dd></div>
        <div><dt>目录／记录更新时间</dt><dd>{time(dataset.updated_at)}</dd></div>
      </dl>
      <p className="qf-updates-note">目录状态不证明任意请求完整；记录更新时间不代表业务数据截至日。</p>
    </DataSheet>
    <DataSheet title="最近更新结果">
      {update ? <>
        <dl className="qf-assets-properties">
          <div><dt>最近处理结果</dt><dd>{updateStatus(update).label}</dd></div>
          <div><dt>处理是否完成</dt><dd>{update.complete ? "已完成（服务端声明）" : "未完成（服务端声明）"}</dd></div>
          <div><dt>本次质量确认</dt><dd>{update.qualified ? "已声明合格（本次处理范围）" : "未获合格确认"}</dd></div>
          <div><dt>来源记录数</dt><dd className="qf-updates-number">{count(update.source_rows)}</dd></div>
          <div><dt>已提交分区数</dt><dd className="qf-updates-number">{count(update.committed_partitions)}</dd></div>
          <div><dt>处理记录更新时间</dt><dd>{time(update.updated_at)}</dd></div>
        </dl>
        <p className="qf-updates-note">处理完成、质量确认、业务覆盖与当前读取权限分别判断。</p>
        {explanation && <div className="qf-updates-explanation"><CircleAlert size={18} aria-hidden="true" />
          <div><strong>{explanation.label}</strong><p>{explanation.next}</p></div>
        </div>}
        {!update.complete && <p>最近更新未完成，不据此宣告全部已有数据失效。</p>}
        <details><summary>更新诊断信息</summary><dl className="qf-assets-properties">
          <div><dt>状态码</dt><dd><code>{updateStatus(update).diagnosticCode ?? "未提供安全状态码"}</code></dd></div>
          <div><dt>原因码</dt><dd><code>{explanation?.code ?? "未提供安全原因码"}</code></dd></div>
        </dl></details>
      </> : <p>暂无更新记录。服务端未提供运行阶段或刷新细节。</p>}
      <details><summary>能力与既有限制</summary>
        <p>旧限制记录数：{count(dataset.legacy_restrictions)}。旧限制仍由服务端判断，不绕过查询限制。</p>
        {dataset.limitations.length ? <ul>{dataset.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul>
          : <p>服务端未提供具体限制说明；这不代表没有限制。</p>}
      </details>
    </DataSheet>
  </>;
}

export function IssueRecord({ issue, ordinal }: { issue: CurrentIssue; ordinal: number }) {
  const reason = issueExplanation(issue.reason);
  const kind = issue.kind === "current" ? "当前限制" : issue.kind === "legacy" ? "既有数据限制" : "限制类型待确认";
  return <li>
    <div className="qf-updates-record-heading"><strong>{ordinal}. {reason.label}</strong><span>{kind}</span></div>
    <p>{reason.next}</p>
    <div className="qf-updates-record-meta"><span>本条受影响成员数：{count(issue.affected_objects)}</span>
      <span>记录更新时间：{time(issue.updated_at)}</span></div>
    {issue.dataset === null && <p className="qf-updates-note">此记录未指定数据集，由服务端纳入本次筛选。</p>}
    <details><summary>问题诊断信息（第 {ordinal} 条）</summary><dl className="qf-assets-properties">
      <div><dt>原因码</dt><dd><code>{reason.code ?? "未提供安全原因码"}</code></dd></div>
      <div><dt>限制类型</dt><dd><code>{/^[a-z][a-z0-9_-]{0,63}$/.test(issue.kind) ? issue.kind : "未提供安全类型码"}</code></dd></div>
      <div><dt>范围标识</dt><dd><code>{issue.scope_key ?? "未声明"}</code></dd></div>
      <div><dt>数据集标识</dt><dd><code>{issue.dataset ?? "未指定数据集"}</code></dd></div>
    </dl></details>
  </li>;
}

const empty: IssuesSnapshot = { result: null, page: 0, offset: 0, readAt: null, stale: false };

/** Fixed D02 export. Same-object tab switches retain this page; identity/generation changes reset it. */
export function UpdateIssuesPanel({ dataset, refreshVersion }: UpdateIssuesPanelProps) {
  const [params] = useSearchParams();
  const key = JSON.stringify([dataset.dataset, dataset.generation]);
  const [view, setView] = useState<{ key: string; snapshot: IssuesSnapshot }>({ key, snapshot: empty });
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const active = useRef<{ key: string; session: ReturnType<typeof createUpdateIssuesSession> } | null>(null);
  const request = useRef(0), busy = useRef(false);
  const clear = useCallback(() => {
    request.current++; busy.current = false; active.current?.session.reset();
    setView(previous => ({ ...previous, snapshot: empty })); setLoading(false);
  }, []);
  const failure = useDataAssetsFailure(clear, setError);
  const load = useCallback(async (page: number, restart = false, force = false) => {
    const current = active.current;
    if (!current || current.key !== key || (busy.current && !force)) return;
    const ticket = ++request.current;
    busy.current = true; setLoading(true); setError("");
    const publish = () => setView({ key, snapshot: current.session.getSnapshot() });
    try { const pending = current.session.read(page, restart); publish(); await pending; }
    catch (problem) {
      if (ticket !== request.current || current !== active.current || isCancellation(problem)) return;
      setError(problem instanceof DataStoreApiError && problem.code === "DATA_CHANGED"
        ? "问题列表在分页期间发生变化，旧分页已清除，请重新读取问题。" : failure(problem));
    } finally {
      if (ticket === request.current && current === active.current) {
        publish(); busy.current = false; setLoading(false);
      }
    }
  }, [key, failure]);
  useEffect(() => {
    if (active.current?.key !== key) {
      active.current?.session.dispose();
      active.current = { key, session: createUpdateIssuesSession(dataset.dataset) };
      setView({ key, snapshot: empty });
    }
    void load(0, true, true);
    return () => { request.current++; busy.current = false; active.current?.session.cancel(); };
  }, [key, dataset.dataset, refreshVersion, load]);
  useEffect(() => () => { active.current?.session.dispose(); active.current = null; }, []);
  const snapshot = view.key === key ? view.snapshot : empty;
  // An earlier empty page must not become a "zero problems" success during a failed refresh.
  const issues = snapshot.result && (snapshot.result.total > 0 || (!error && !loading && !snapshot.stale))
    ? snapshot.result : null;
  return <div className="qf-updates">
    <UpdateSummary dataset={dataset} />
    <DataSheet title="当前问题与限制" className="qf-updates-issues">
      <div className="qf-updates-actions">
        <p className="qf-updates-note">每页 20 条，按需读取。分页期间问题可能变化，可重新读取第一页。</p>
        <button className="qfo-secondary-btn" type="button" disabled={loading} onClick={() => void load(0, true)}>
          <RefreshCw size={16} aria-hidden="true" />{loading ? "读取问题中…" : "重新读取问题"}
        </button>
      </div>
      {error && <LocalNotice tone="error">问题读取失败：{error}{issues && ` 保留上次成功读取的第 ${snapshot.page + 1} 页；不能据此判断当前问题已清除。`}</LocalNotice>}
      {snapshot.stale && issues && <LocalNotice tone="warning">保留上次问题列表，分页已重置。重新读取第一页成功后才能继续翻页。</LocalNotice>}
      <div aria-busy={loading}>
        {loading && <p role="status">{issues ? "正在读取问题，暂保留上次列表…" : "正在读取当前问题…"}</p>}
        {issues && <>
          <dl className="qf-updates-counts">
            <div><dt>问题记录数（筛选总数）</dt><dd>{count(issues.total)}</dd></div>
            <div><dt>受影响成员数（筛选合计）</dt><dd>{count(issues.affected_objects)}</dd></div>
            <div><dt>当页问题记录数</dt><dd>{count(issues.items.length)}</dd></div>
          </dl>
          <p className="qf-updates-note">成员可能在多条记录中重叠；合计不是去重对象数或错误率。当页不代表全域原因分布。</p>
          <p className="qf-updates-note">问题列表读取于：{time(snapshot.readAt)}</p>
          {issues.total === 0 ? <p role="status">当前筛选没有已登记的问题记录；不代表业务覆盖或质量已经确认。</p>
            : <><p role="status">第 {snapshot.page + 1} 页 · 记录 {snapshot.offset + 1}–{snapshot.offset + issues.items.length} / {count(issues.total)}</p>
              <ul className="qf-updates-records">{issues.items.map((issue, index) => <IssueRecord
                key={`${snapshot.offset}-${index}`} issue={issue} ordinal={snapshot.offset + index + 1} />)}</ul>
              <nav className="qf-assets-pager" aria-label="问题分页">
                <button className="qfo-secondary-btn" type="button" disabled={loading || snapshot.stale || snapshot.page === 0}
                  onClick={() => void load(snapshot.page - 1)}>上一页问题</button>
                <button className="qfo-secondary-btn" type="button" disabled={loading || snapshot.stale || issues.next_offset === null}
                  onClick={() => void load(snapshot.page + 1)}>下一页问题</button>
              </nav></>}
        </>}
      </div>
    </DataSheet>
    <DataSheet title="只读下一步"><div className="qf-updates-links">
      <Link to={`?${viewParams(params, "preview")}`}>调整当前查询</Link>
      <Link to="/admin/data-sources">查看已有数据源</Link><Link to="/admin/tasks">查看已有采集任务</Link>
    </div><p className="qf-updates-note">本页刷新只读取已登记状态和问题；链接仅导航。</p></DataSheet>
  </div>;
}
