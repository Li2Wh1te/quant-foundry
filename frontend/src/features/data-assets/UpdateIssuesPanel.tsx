import { useCallback, useEffect, useRef, useState } from "react";
import { createRequestScope, dataAssetsClient, formatCount as count, formatTimestamp as time,
  isCancellation, issueReason, updateStatus, type CurrentDataset, type IssueList, type RequestScope } from "./data";
import { DataSheet, LocalNotice } from "./components";
import { useDataAssetsFailure } from "./components/useDataAssetsFailure";

export interface UpdateIssuesPanelProps { dataset: CurrentDataset; refreshVersion: number; }

/** D06 owns update explanations and issue pagination behind this fixed export. */
export function UpdateIssuesPanel({ dataset, refreshVersion }: UpdateIssuesPanelProps) {
  const [issues, setIssues] = useState<IssueList | null>(null);
  const [issuesError, setIssuesError] = useState("");
  const [loading, setLoading] = useState(false);
  const active = useRef<RequestScope | null>(null);
  const clear = useCallback(() => {
    active.current?.cancel(); setIssues(null); setLoading(false);
  }, []);
  const failure = useDataAssetsFailure(clear, setIssuesError);
  useEffect(() => {
    if (!dataset.issues && !dataset.legacy_restrictions) {
      setIssues(null); setIssuesError(""); setLoading(false);
      return;
    }
    const scope = createRequestScope();
    active.current = scope;
    let disposed = false;
    setIssuesError(""); setLoading(true);
    scope.run(signal => dataAssetsClient.listIssues(dataset.dataset, { limit: 20, signal }))
      .then(result => { if (!disposed) setIssues(result); })
      .catch(problem => { if (!disposed && !isCancellation(problem)) setIssuesError(failure(problem)); })
      .finally(() => { if (!disposed && active.current === scope) setLoading(false); });
    return () => { disposed = true; scope.dispose(); };
  }, [dataset.dataset, dataset.issues, dataset.legacy_restrictions, refreshVersion, failure]);
  return <>
      {issuesError && <LocalNotice tone="error">问题详情读取失败：{issuesError}{issues && " 当前保留上次读取的问题列表。"}</LocalNotice>}
        <DataSheet title="最近更新与能力限制">
          {dataset.last_update ? <dl className="qf-assets-properties">
            <div><dt>最近更新</dt><dd>{updateStatus(dataset.last_update).label}</dd></div>
            <div><dt>来源记录</dt><dd>{count(dataset.last_update.source_rows)}</dd></div>
            <div><dt>提交分区</dt><dd>{count(dataset.last_update.committed_partitions)}</dd></div>
            <div><dt>执行时间</dt><dd>{time(dataset.last_update.updated_at)}</dd></div>
          </dl> : <p>尚无本地处理记录。</p>}
          {dataset.last_update && !dataset.last_update.complete &&
            <p className="qf-assets-warning">最近一次更新未完成；已有当前数据是否可用请以当前数据状态和查询结果为准。</p>}
          {!!dataset.legacy_restrictions && <p className="qf-assets-warning">旧数据限制仍待定位或处理，相关查询不会绕过限制。</p>}
          {dataset.limitations.length > 0 && <details><summary>已知能力边界</summary>
            <ul>{dataset.limitations.map(item => <li key={item}>{item}</li>)}</ul>
          </details>}
        </DataSheet>
      {loading && !issues && <div className="qf-assets-loading" role="status">正在读取当前问题…</div>}
      {!loading && !issuesError && !(issues?.items.length) && <DataSheet title="当前问题与限制"><p role="status">{dataset.issues === 0 && !dataset.legacy_restrictions ? "当前没有已登记的问题。" : "问题状态待确认。"}</p></DataSheet>}
      {(issues?.items.length ?? 0) > 0 && <DataSheet title="当前问题与限制"><p>显示最近 {issues?.items.length} 项；共 {issues?.total} 项。</p>
        <ul className="qf-assets-issues">{issues?.items.map((issue, index) => <li key={`${issue.kind}-${issue.scope_key}-${index}`}>
          <strong>{issueReason(issue.reason).label}</strong>
          <span>{issue.scope_key ?? "范围待定位"} · {time(issue.updated_at)}</span>
          <details><summary>技术原因</summary><code>{issueReason(issue.reason).diagnosticCode ?? "未声明"}</code></details>
        </li>)}</ul>
      </DataSheet>}
  </>;
}
