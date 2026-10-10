import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { createRequestScope, currentStatus, dataAssetsClient, formatCount as count,
  formatTimestamp as time, frequencyLabel, isCancellation, type CatalogSnapshot, type RequestScope } from "./data";
import { Select } from "../../components/controls/Select";
import { DataSheet, LocalNotice, WorkspaceHeader } from "./components";
import { useDataAssetsFailure } from "./components/useDataAssetsFailure";
import { catalogParams, datasetHref, restoreCatalogScroll, saveCatalogScroll } from "./navigation";

const PAGE_SIZE = 20;

export function CatalogView() {
  const [params, setParams] = useSearchParams();
  const restoredScroll = useRef(false);
  const [catalog, setCatalog] = useState<CatalogSnapshot | null>(null);
  const [loadedAt, setLoadedAt] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const active = useRef<RequestScope | null>(null);
  const clear = useCallback(() => {
    active.current?.cancel(); setCatalog(null); setLoadedAt(null); setBusy(false);
  }, []);
  const failure = useDataAssetsFailure(clear, setError);

  useEffect(() => {
    const scope = createRequestScope();
    active.current = scope;
    let disposed = false;
    setBusy(true);
    setError("");
    scope.run(signal => dataAssetsClient.loadCatalog({ signal }))
      .then(result => {
        if (disposed) return;
        setCatalog(result);
        setLoadedAt(result.readAt);
        if (result.error) setError(failure(result.error));
      })
      .catch(problem => {
        if (!disposed && !isCancellation(problem)) setError(failure(problem));
      })
      .finally(() => {
        if (!disposed && active.current === scope) setBusy(false);
      });
    return () => { disposed = true; scope.dispose(); };
  }, [refresh, failure]);

  useEffect(() => {
    if (!catalog || restoredScroll.current) return;
    const frame = requestAnimationFrame(() => {
      restoreCatalogScroll(params);
      restoredScroll.current = true;
    });
    return () => cancelAnimationFrame(frame);
  }, [catalog, params]);

  const search = params.get("search") ?? "";
  const filter = params.get("status") ?? "all";
  const requestedPage = Number(params.get("page") ?? "1");
  const page = Number.isInteger(requestedPage) && requestedPage > 0 ? requestedPage : 1;
  const filtered = catalog?.items.filter(item => {
    const matchingText = `${item.name} ${item.dataset}`.toLowerCase().includes(search.trim().toLowerCase());
    return matchingText && (filter === "all" || item.status === filter);
  }) ?? [];
  const pages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const visiblePage = Math.min(page, pages);
  const visible = filtered.slice((visiblePage - 1) * PAGE_SIZE, visiblePage * PAGE_SIZE);
  const statusCount = (status: string) => catalog?.complete ? catalog.counts?.byStatus[status] ?? 0 : null;

  function update(key: string, value: string) {
    const next = catalogParams(params);
    next.set(key, value);
    next.set("page", "1");
    setParams(next);
  }
  function go(pageNumber: number) {
    const next = catalogParams(params);
    next.set("page", String(pageNumber));
    setParams(next);
  }

  return <section className="qf-assets" aria-busy={busy}>
    <WorkspaceHeader title="数据资产" eyebrow="CURRENT DATA / 01"
      description="查看当前数据集、最近更新结果与限制。"
      actions={<button className="qfo-secondary-btn" type="button" onClick={() => setRefresh(value => value + 1)} disabled={busy}>
        <RefreshCw size={16} aria-hidden="true" />{busy ? "刷新中…" : "刷新页面"}
      </button>} />
    {catalog?.phase !== "ready" && catalog &&
      <LocalNotice tone="warning">数据底座处于维护状态。目录可查看，当前数据读取以查询响应为准。</LocalNotice>}
    {error && <LocalNotice tone="error">{error}{catalog && ` 当前保留 ${time(loadedAt)} 读取的目录。`}</LocalNotice>}
    {!catalog && !error && <div className="qf-assets-loading" role="status">正在读取当前数据目录…</div>}
    {catalog && <>
      <div className="qf-assets-metrics" aria-label="数据目录摘要">
        <div><span>已加载数据集</span><strong>{count(catalog.items.length)}</strong></div>
        <div><span>当前可用</span><strong>{count(statusCount("available"))}</strong></div>
        <div><span>需要处理</span><strong>{count(catalog.complete ? (statusCount("restricted") ?? 0) + (statusCount("rebuild_required") ?? 0) : null)}</strong></div>
        <div><span>尚未检查</span><strong>{count(statusCount("not_checked"))}</strong></div>
      </div>
      {!catalog.complete && <LocalNotice tone="warning">目录未完整加载，筛选仅涵盖已加载的数据集，完整状态计数未声明。</LocalNotice>}
      <div className="qf-assets-toolbar">
        <label>搜索数据集<input value={search} placeholder="名称或数据集标识" onChange={event => update("search", event.target.value)} /></label>
        <label>当前状态<Select value={filter} onChange={event => update("status", event.target.value)}>
          <option value="all">全部</option>
          <option value="available">当前可用</option>
          <option value="empty">当前为空</option>
          <option value="not_checked">尚未检查</option>
          <option value="restricted">存在限制</option>
          <option value="rebuild_required">需要重建</option>
          <option value="rebuilding">维护重建中</option>
        </Select></label>
      </div>
      {visible.length ? <DataSheet className="qf-assets-catalog-sheet"><div className="qf-assets-scroll" tabIndex={0} role="region" aria-label="数据目录表格"><table>
        <thead><tr><th>数据集</th><th>频率 / 口径</th><th>当前分区范围</th><th className="qf-assets-number">当前记录</th><th>更新时间</th><th>状态</th></tr></thead>
        <tbody>{visible.map(item => <tr key={item.dataset}>
          <td><Link onClick={() => saveCatalogScroll(params)} to={datasetHref(item.dataset, params)}>{item.name}</Link><small><code>{item.dataset}</code></small></td>
          <td>{frequencyLabel(item.frequency)}<small>{item.source} · 类型化对象</small></td>
          <td>{item.partition_range.from ?? "未检查"}{item.partition_range.to && ` ～ ${item.partition_range.to}`}<small>按存储分区统计</small></td>
          <td className="qf-assets-number">{count(item.row_count)}</td>
          <td>{time(item.updated_at)}</td>
          <td><span className={`qf-assets-state state-${item.status}`}>{currentStatus(item.status).label}</span></td>
        </tr>)}</tbody>
      </table></div></DataSheet> : <div className="qf-assets-empty" role="status">
        {catalog.items.length ? "没有符合筛选条件的数据集，请调整搜索或状态。" : "当前目录未登记业务数据集。"}
      </div>}
      <div className="qf-assets-pager">
        <span>第 {visiblePage} / {pages} 页 · 共 {filtered.length} 个数据集</span>
        <button className="qfo-secondary-btn" type="button" onClick={() => go(visiblePage - 1)} disabled={visiblePage === 1}>上一页</button>
        <button className="qfo-secondary-btn" type="button" onClick={() => go(visiblePage + 1)} disabled={visiblePage === pages}>下一页</button>
      </div>
      <p className="qf-assets-asof">目录读取于 {time(loadedAt)}。分区范围不等于逐条业务日期覆盖。</p>
    </>}
  </section>;
}
