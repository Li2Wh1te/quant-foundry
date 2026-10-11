import { CheckCircle2, CircleAlert, CircleHelp, Clock3, Copy, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigationType, useSearchParams } from "react-router-dom";
import { createRequestScope, currentStatus, dataAssetsClient, datasetName, DataStoreApiError, formatCount as count,
  formatTimestamp as time, frequencyLabel, isCancellation, updateStatus,
  type CatalogSnapshot, type CurrentDataset, type RequestScope, type StatusPresentation } from "./data";
import { Select } from "../../components/controls/Select";
import { DataSheet, LocalNotice, WorkspaceHeader } from "./components";
import { useDataAssetsFailure } from "./components/useDataAssetsFailure";
import { catalogParams, datasetHref, restoreCatalogScroll, saveCatalogScroll } from "./navigation";
import "./CatalogView.css";

const PAGE_SIZE = 20;
const collator = new Intl.Collator("zh-CN", { numeric: true, sensitivity: "base" });
const knownStatuses = ["available", "restricted", "not_checked", "empty", "rebuild_required", "rebuilding"];

/** Compatibility export; every view uses D01's shared display alias. */
export const catalogName = datasetName;

function normalized(value: string): string { return value.normalize("NFKC").trim().toLocaleLowerCase("zh-CN"); }

export function catalogPage(value: string | null): number {
  const page = value !== null && /^[1-9]\d*$/.test(value) ? Number(value) : 1;
  return Number.isSafeInteger(page) ? page : 1;
}

export function catalogItems(items: readonly CurrentDataset[], params: URLSearchParams): CurrentDataset[] {
  const search = normalized(params.get("search") ?? "");
  const status = params.get("status") || "all", source = params.get("source") || "all";
  const frequency = params.get("frequency") || "all";
  return items.filter(item => normalized(`${catalogName(item)} ${item.name} ${item.dataset}`).includes(search)
    && (status === "all" || (status === "unknown" ? !currentStatus(item.status).known : item.status === status))
    && (source === "all" || item.source === source)
    && (frequency === "all" || item.frequency === frequency))
    .sort((a, b) => collator.compare(catalogName(a), catalogName(b)) || collator.compare(a.dataset, b.dataset)
      || (a.dataset < b.dataset ? -1 : a.dataset > b.dataset ? 1 : 0));
}

function sourceLabel(value: string): string {
  const sources: Record<string, string> = { tonghuashun: "同花顺", tushare: "Tushare", synthetic: "合成来源" };
  return Object.hasOwn(sources, value) ? sources[value] : value && value !== "unknown" ? value : "未声明";
}

export function catalogOptions(items: readonly CurrentDataset[], kind: "source" | "frequency", selected: string) {
  const label = (value: string) => kind === "source" ? sourceLabel(value)
    : frequencyLabel(value) === "未声明" && value && value !== "unknown" ? `未声明（${value}）` : frequencyLabel(value);
  const values = new Set(items.map(item => item[kind]));
  // Keep a URL selection visible even after a refresh or a partial load removes
  // its option. It still filters to zero matches until the user changes it.
  if (selected !== "all") values.add(selected);
  return [...values].sort((a, b) => collator.compare(label(a), label(b)) || collator.compare(a, b))
    .map(value => ({ value, label: label(value) }));
}

export function catalogMetrics(snapshot: CatalogSnapshot | null) {
  if (!snapshot?.complete || !snapshot.counts) return ["待加载", "待加载", "待加载", "待加载"];
  const states = snapshot.counts.byStatus;
  return [count(snapshot.counts.total), count(states.available ?? 0), count(states.restricted ?? 0), count(states.not_checked ?? 0)];
}

function incompleteMessage(snapshot: CatalogSnapshot): string {
  return ({ budget: "本次读取已达到目录元数据加载预算。", failed: "后续目录读取中断。",
    changed: "读取期间目录发生变化，请重新读取。", pagination: "服务端分页信息不一致，请重试。" })[snapshot.incompleteReason ?? "failed"];
}

/** A failed refresh must not replace an existing observation with fewer rows. */
export function retainCatalog(previous: CatalogSnapshot | null, next: CatalogSnapshot): boolean {
  return previous !== null && (next.error !== null || (next.incompleteReason !== null && next.incompleteReason !== "budget"));
}

export function CatalogFact({ fact }: { fact: StatusPresentation }) {
  const Icon = fact.tone === "success" ? CheckCircle2 : fact.tone === "warning" || fact.tone === "danger"
    ? CircleAlert : fact.tone === "info" ? Clock3 : CircleHelp;
  return <span className={`qf-catalog-fact tone-${fact.tone}`}><Icon size={16} aria-hidden="true" />{fact.label}</span>;
}

export function CatalogRow({ item, params }: { item: CurrentDataset; params: URLSearchParams }) {
  const [copied, setCopied] = useState("");
  const name = catalogName(item);
  async function copy() {
    try { await navigator.clipboard.writeText(item.dataset); setCopied("已复制"); }
    catch { setCopied("复制失败，可选取下方标识"); }
  }
  return <tr>
    <td>
      <Link onClick={() => saveCatalogScroll(params)} to={datasetHref(item.dataset, params)}>{name}</Link>
      <small>用途未声明</small>
      <details className="qf-catalog-technical"><summary>技术信息</summary>
        <dl>
          <div><dt>数据集标识</dt><dd><code>{item.dataset}</code>
            <button className="qfo-secondary-btn" type="button" onClick={() => void copy()} aria-label={`复制 ${name} 的数据集标识`}><Copy size={14} aria-hidden="true" />复制标识</button>
            <span role="status">{copied}</span></dd></div>
          <div><dt>原始名称</dt><dd>{item.name}</dd></div>
          <div><dt>来源 / 频率</dt><dd><code>{item.source || "未声明"} / {item.frequency || "未声明"}</code></dd></div>
          <div><dt>状态原值</dt><dd><code>{item.status}</code></dd></div>
          <div><dt>存储布局</dt><dd><code>{item.representation || "未声明"}</code></dd></div>
          <div><dt>Schema / 规则</dt><dd><code>{item.schema_id || "未声明"} / {item.rule || "未声明"}</code></dd></div>
          <div><dt>存储分区范围</dt><dd>{item.partition_range.from ?? "未声明"} ～ {item.partition_range.to ?? "未声明"}</dd></div>
          <div><dt>存储更新时间</dt><dd>{time(item.updated_at)}</dd></div>
          <div><dt>最近更新原值</dt><dd><code>{item.last_update?.state ?? "暂无更新记录"}</code></dd></div>
        </dl>
      </details>
    </td>
    <td>{frequencyLabel(item.frequency)}</td>
    <td>{sourceLabel(item.source)}<small>{item.representation === "typed-object-nodes-v2" ? "类型化对象节点" : "存储口径未声明"}</small></td>
    <td><CatalogFact fact={currentStatus(item.status)} /></td>
    <td><CatalogFact fact={updateStatus(item.last_update)} />
      {item.last_update && <small>记录时间：{time(item.last_update.updated_at)}</small>}</td>
    <td className="qf-assets-number qf-catalog-secondary">{count(item.row_count)}</td>
  </tr>;
}

export function CatalogView() {
  const [params, setParams] = useSearchParams();
  const navigationType = useNavigationType();
  const context = catalogParams(params).toString();
  const latestParams = useRef(catalogParams(params));
  const issuedContexts = useRef(new Set<string>());
  const [searchInput, setSearchInput] = useState(params.get("search") ?? "");
  const restoredContext = useRef<string | null>(null);
  const latestCatalog = useRef<CatalogSnapshot | null>(null);
  const [catalog, setCatalog] = useState<CatalogSnapshot | null>(null);
  const [error, setError] = useState<{ message: string; retained: boolean; denied?: boolean } | null>(null);
  const [busy, setBusy] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const active = useRef<RequestScope | null>(null);
  const clear = useCallback(() => {
    active.current?.cancel(); latestCatalog.current = null;
    setCatalog(null); setBusy(false); restoredContext.current = null;
  }, []);
  const report = useCallback((message: string) => setError({ message, retained: false, denied: true }), []);
  const failure = useDataAssetsFailure(clear, report);

  useLayoutEffect(() => {
    // Router transitions may publish an earlier URL after the next input has
    // arrived. Preserve the latest local edit through our own pending changes;
    // browser history and external navigation still restore their URL state.
    if (navigationType !== "POP" && issuedContexts.current.has(context)) {
      if (context === latestParams.current.toString()) issuedContexts.current.clear();
      return;
    }
    issuedContexts.current.clear(); latestParams.current = catalogParams(params);
    setSearchInput(params.get("search") ?? "");
  }, [context, params, navigationType]);

  useEffect(() => {
    const scope = createRequestScope();
    active.current = scope;
    let disposed = false;
    setBusy(true); setError(null);
    scope.run(signal => dataAssetsClient.loadCatalog({ signal }))
      .then(result => {
        if (disposed) return;
        if (retainCatalog(latestCatalog.current, result)) {
          setError({ message: result.error ? failure(result.error) : incompleteMessage(result), retained: true });
        } else {
          latestCatalog.current = result; setCatalog(result);
          if (result.error) setError({ message: failure(result.error), retained: false });
        }
      })
      .catch(problem => {
        if (!disposed && !isCancellation(problem)) {
          const message = failure(problem);
          if (problem instanceof DataStoreApiError && [401, 403].includes(problem.status)) return;
          setError({ message, retained: latestCatalog.current !== null });
        }
      })
      .finally(() => { if (!disposed && active.current === scope) setBusy(false); });
    return () => { disposed = true; scope.dispose(); };
  }, [refresh, failure]);

  const search = params.get("search") ?? "", status = params.get("status") || "all";
  const source = params.get("source") || "all", frequency = params.get("frequency") || "all";
  const filtered = useMemo(() => catalogItems(catalog?.items ?? [], params), [catalog, params]);
  const pages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const visiblePage = Math.min(catalogPage(params.get("page")), pages);
  const visible = filtered.slice((visiblePage - 1) * PAGE_SIZE, visiblePage * PAGE_SIZE);
  const metrics = catalogMetrics(catalog);
  const filtersActive = !!search || [status, source, frequency].some(value => value !== "all");

  useEffect(() => {
    // Correct invalid/shrunken pages in the URL only after this read finishes.
    // Replacing this entry avoids leaving a broken page in browser history.
    if (!catalog || busy || latestParams.current.toString() !== context
      || !params.has("page") || params.get("page") === String(visiblePage)) return;
    const next = catalogParams(params); next.set("page", String(visiblePage));
    setParams(next, { replace: true });
  }, [catalog, busy, context, params, visiblePage, setParams]);

  useLayoutEffect(() => {
    if (!catalog || busy || restoredContext.current === context
      || (params.has("page") && params.get("page") !== String(visiblePage))) return;
    // Restore before paint, so a queued animation frame cannot move the next
    // pagination button between pointer-down and pointer-up during rapid use.
    restoreCatalogScroll(params); restoredContext.current = context;
  }, [catalog, busy, context, params, visiblePage]);

  function navigateCatalog(next: URLSearchParams) {
    latestParams.current = next; issuedContexts.current.add(next.toString());
    // A controlled text input must update outside the router's transition.
    setSearchInput(next.get("search") ?? ""); setParams(next);
  }
  function update(key: string, value: string) {
    saveCatalogScroll(latestParams.current);
    const next = catalogParams(latestParams.current); next.set(key, value); next.set("page", "1");
    navigateCatalog(next);
  }
  function go(page: number) {
    saveCatalogScroll(latestParams.current);
    const next = catalogParams(latestParams.current); next.set("page", String(page)); navigateCatalog(next);
  }
  function reset() { saveCatalogScroll(latestParams.current); navigateCatalog(new URLSearchParams({ page: "1" })); }
  function retry() { setRefresh(value => value + 1); }

  return <section className="qf-assets qf-catalog" aria-busy={busy}>
    <WorkspaceHeader title="数据资产" eyebrow="CURRENT DATA / 01" description="找到当前数据集，分别查看当前状态与最近更新结果。"
      actions={<button className="qfo-secondary-btn" type="button" onClick={retry} disabled={busy}>
        <RefreshCw size={16} aria-hidden="true" />刷新页面
      </button>} />
    <div className="qf-assets-metrics" aria-label="数据目录摘要">
      {["已登记", "服务端标为可用", "存在限制", "尚未检查"].map((label, index) =>
        <div key={label}><span>{label}</span><strong className={metrics[index] === "待加载" ? "qf-assets-metric-label" : undefined}>{metrics[index]}</strong></div>)}
    </div>
    {busy && <LocalNotice tone="info">{catalog ? "正在重新读取目录，当前仍显示上次读取结果。" : "正在读取当前数据目录…"}</LocalNotice>}
    {catalog && catalog.phase !== "ready" && <LocalNotice tone="warning">
      {catalog.phase === "unknown" ? "数据底座状态待确认。" : "数据底座处于维护状态。"}目录可查看，当前数据读取以查询响应为准。
    </LocalNotice>}
    {error && <LocalNotice tone="error">
      {error.denied ? "目录读取被拒绝。" : error.retained ? "刷新目录失败。" : catalog ? "目录读取中断。" : "首次读取目录失败。"}{error.message}
      {error.retained && catalog && ` 当前保留 ${time(catalog.readAt)} 读取的${catalog.complete ? "" : "部分"}目录。`}
      {!catalog && <button className="qfo-secondary-btn" type="button" onClick={retry} disabled={busy}>重试读取目录</button>}
    </LocalNotice>}
    {catalog && !catalog.complete && <LocalNotice tone="warning">
      目录未完整加载：已加载 {count(catalog.items.length)} 个数据集。{incompleteMessage(catalog)}
      筛选仅涵盖已加载范围，全局分组计数待加载。
      <button className="qfo-secondary-btn" type="button" onClick={retry} disabled={busy}>重试完整加载</button>
    </LocalNotice>}
    <div className="qf-assets-toolbar qf-catalog-toolbar">
      <label>搜索数据集<input value={searchInput} placeholder="中文名称、原名或标识" onChange={event => update("search", event.target.value)} /></label>
      <label>当前状态<Select aria-label="当前状态" value={status} onChange={event => update("status", event.target.value)}>
        <option value="all">全部状态</option>
        {knownStatuses.map(value => <option key={value} value={value}>{currentStatus(value).label}</option>)}
        <option value="unknown">状态待确认</option>
        {!["all", "unknown", ...knownStatuses].includes(status) && <option value={status}>状态待确认（{status}）</option>}
      </Select></label>
      <label>来源<Select aria-label="来源" value={source} onChange={event => update("source", event.target.value)}>
        <option value="all">全部来源</option>
        {catalogOptions(catalog?.items ?? [], "source", source).map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
      </Select></label>
      <label>频率<Select aria-label="频率" value={frequency} onChange={event => update("frequency", event.target.value)}>
        <option value="all">全部频率</option>
        {catalogOptions(catalog?.items ?? [], "frequency", frequency).map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
      </Select></label>
      {filtersActive && <button className="qfo-secondary-btn" type="button" onClick={reset}>清除筛选</button>}
    </div>
    {catalog ? <>
      <p className="qf-catalog-results" role="status">{catalog.complete ? "全部目录" : "已加载范围内筛选"} · {count(filtered.length)} 个匹配 · 每页 {PAGE_SIZE} 项</p>
      {visible.length ? <DataSheet className="qf-assets-catalog-sheet">
        <div className="qf-assets-scroll" tabIndex={0} role="region" aria-label="数据目录表格"><table>
          <caption className="qf-catalog-caption">当前状态与最近更新结果分别由服务端声明</caption>
          <thead><tr><th scope="col">数据集 / 用途</th><th scope="col">频率</th><th scope="col">来源 / 存储口径</th>
            <th scope="col">当前状态</th><th scope="col">最近更新结果</th><th scope="col" className="qf-assets-number qf-catalog-secondary">存储行数</th></tr></thead>
          <tbody>{visible.map(item => <CatalogRow key={item.dataset} item={item} params={params} />)}</tbody>
        </table></div>
      </DataSheet> : <div className="qf-assets-empty" role="status">
        {!catalog.items.length && catalog.complete ? <><strong>当前目录未登记业务数据集。</strong><p>可刷新页面重新读取目录。</p></>
          : !catalog.items.length ? <><strong>当前尚无已加载的数据集。</strong><p>目录未完整加载，请重试读取。</p></>
          : <><strong>{catalog.complete ? "没有符合筛选条件的数据集。" : "已加载范围内没有符合筛选条件的数据集。"}</strong>
            <p>可调整名称、状态、来源或频率。{!catalog.complete && "未加载部分尚未参与筛选。"}</p>
            <button className="qfo-secondary-btn" type="button" onClick={reset}>清除筛选</button></>}
      </div>}
      <nav className="qf-assets-pager" aria-label="数据目录分页">
        <span>第 {visiblePage} / {pages} 页 · {catalog.complete ? "全部目录" : "已加载范围"} {count(filtered.length)} 项</span>
        <button className="qfo-secondary-btn" type="button" onClick={() => go(visiblePage - 1)} disabled={visiblePage === 1}>上一页</button>
        <button className="qfo-secondary-btn" type="button" onClick={() => go(visiblePage + 1)} disabled={visiblePage === pages}>下一页</button>
      </nav>
      <p className="qf-assets-asof">页面读取于 {time(catalog.readAt)}。存储行数是物理行；单位、价格口径和业务截至日未声明。</p>
    </> : !error && <div className="qf-assets-loading" aria-hidden="true">目录读取完成后显示数据集、当前状态与最近更新结果。</div>}
  </section>;
}
