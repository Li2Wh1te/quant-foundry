import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import {
  dataStoreApi, DataStoreApiError,
  type CurrentDataset, type DatasetList, type IssueList,
  type PreviewRequest, type PreviewResult
} from "../api/dataStore";
import { useAuth } from "../auth/AuthContext";
import { Select } from "../components/controls/Select";
import "./DataAssets.css";

const ROOT = "/admin/data-assets";
const PAGE_SIZE = 20;
const statusNames: Record<string, string> = {
  rebuilding: "维护重建中",
  not_checked: "尚未检查",
  empty: "当前为空",
  available: "当前可用",
  restricted: "存在限制",
  rebuild_required: "需要重建",
  processed: "处理完成",
  processed_with_issues: "处理完成，存在问题",
  incomplete: "处理未完成",
  running: "处理中"
};
const issueNames: Record<string, string> = {
  LEGACY_RESTRICTION: "旧数据限制待核对",
  SOURCE_CONFIRMATION_UNPROVEN: "来源确认依据不足",
  REPORT_INCOMPLETE: "报告内容不完整",
  ISSUE_BUDGET_EXCEEDED: "问题数量超过处理上限"
};
const frequencies: Record<string, string> = {
  daily: "日频", minute: "分钟", tick: "逐笔", report: "报告", object: "按对象"
};

function time(value: string | null | undefined): string {
  if (!value) return "暂无记录";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "时间未知" : parsed.toLocaleString("zh-CN", { hour12: false });
}

function count(value: number | null | undefined): string {
  return value == null ? "未检查" : value.toLocaleString("zh-CN");
}

function useFailure(clear: () => void) {
  const { logout } = useAuth();
  const navigate = useNavigate();
  return useCallback((error: unknown): string => {
    if (error instanceof DataStoreApiError && [401, 403].includes(error.status)) {
      clear();
      logout();
      navigate("/login", { replace: true });
    }
    return error instanceof Error ? error.message : "读取失败，请稍后重试。";
  }, [clear, logout, navigate]);
}

function saveCatalogScroll() {
  try {
    const main = document.querySelector(".qfo-main");
    sessionStorage.setItem("qf-assets-scroll", String(main?.scrollTop ?? 0));
  } catch { /* Session storage is optional. */ }
}

function Catalog() {
  const [params, setParams] = useSearchParams();
  const [catalog, setCatalog] = useState<DatasetList | null>(null);
  const [loadedAt, setLoadedAt] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const clear = useCallback(() => setCatalog(null), []);
  const failure = useFailure(clear);

  useEffect(() => {
    const controller = new AbortController();
    setBusy(true);
    setError("");
    dataStoreApi<DatasetList>("/datasets?limit=100", controller.signal)
      .then(result => {
        if (controller.signal.aborted) return;
        setCatalog(result);
        setLoadedAt(new Date().toISOString());
      })
      .catch(problem => {
        if (!controller.signal.aborted) setError(failure(problem));
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => controller.abort();
  }, [refresh, failure]);

  useEffect(() => {
    const main = document.querySelector(".qfo-main");
    if (!main || !catalog) return;
    try { main.scrollTop = Number(sessionStorage.getItem("qf-assets-scroll") ?? 0); }
    catch { /* Keep the browser's default scroll position. */ }
  }, [catalog]);

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

  function update(key: string, value: string) {
    const next = new URLSearchParams(params);
    next.set(key, value);
    next.set("page", "1");
    setParams(next);
  }
  function go(pageNumber: number) {
    const next = new URLSearchParams(params);
    next.set("page", String(pageNumber));
    setParams(next);
  }

  return <section className="qf-assets" aria-busy={busy}>
    <header className="qf-assets-heading">
      <div><p className="qf-assets-eyebrow">CURRENT DATA / 01</p><h1>数据资产</h1>
        <p>查看当前数据、处理状态与限制；历史发布和候选流程已退役。</p></div>
      <button className="qfo-secondary-btn" type="button" onClick={() => setRefresh(value => value + 1)} disabled={busy}>
        <RefreshCw size={16} aria-hidden="true" />{busy ? "刷新中…" : "刷新"}
      </button>
    </header>
    {catalog?.phase !== "ready" && catalog &&
      <div className="qf-assets-banner" role="status">数据底座处于维护重建状态。目录可查看，当前数据读取暂不可用。</div>}
    {error && <p role="alert" className="qf-assets-error">{error}{catalog && ` 当前保留 ${time(loadedAt)} 读取的目录。`}</p>}
    {!catalog && !error && <p role="status">正在读取当前数据目录…</p>}
    {catalog && <>
      <div className="qf-assets-metrics" aria-label="数据目录摘要">
        <div><span>已登记数据集</span><strong>{count(catalog.total)}</strong></div>
        <div><span>当前可用</span><strong>{count(catalog.items.filter(item => item.status === "available").length)}</strong></div>
        <div><span>需要处理</span><strong>{count(catalog.items.filter(item => ["restricted", "rebuild_required"].includes(item.status)).length)}</strong></div>
        <div><span>尚未检查</span><strong>{count(catalog.items.filter(item => item.status === "not_checked").length)}</strong></div>
      </div>
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
      {visible.length ? <div className="qf-assets-sheet qf-assets-scroll"><table>
        <thead><tr><th>数据集</th><th>频率 / 口径</th><th>当前分区范围</th><th className="qf-assets-number">当前记录</th><th>更新时间</th><th>状态</th></tr></thead>
        <tbody>{visible.map(item => <tr key={item.dataset}>
          <td><Link onClick={saveCatalogScroll} to={`${ROOT}/${encodeURIComponent(item.dataset)}?${params}`}>{item.name}</Link><small><code>{item.dataset}</code></small></td>
          <td>{frequencies[item.frequency] ?? "未声明"}<small>{item.source} · 类型化对象</small></td>
          <td>{item.partition_range.from ?? "未检查"}{item.partition_range.to && ` ～ ${item.partition_range.to}`}<small>按存储分区统计</small></td>
          <td className="qf-assets-number">{count(item.row_count)}</td>
          <td>{time(item.updated_at)}</td>
          <td><span className={`qf-assets-state state-${item.status}`}>{statusNames[item.status] ?? "状态未知"}</span></td>
        </tr>)}</tbody>
      </table></div> : <div className="qf-assets-empty" role="status">
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

function DatasetDetail({ datasetId }: { datasetId: string }) {
  const [params] = useSearchParams();
  const [dataset, setDataset] = useState<CurrentDataset | null>(null);
  const [issues, setIssues] = useState<IssueList | null>(null);
  const [issuesError, setIssuesError] = useState("");
  const [error, setError] = useState("");
  const [previewError, setPreviewError] = useState("");
  const [busy, setBusy] = useState(false);
  const [reading, setReading] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [preview, setPreview] = useState<PreviewResult | null>(null);
  const [page, setPage] = useState(0);
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [representation, setRepresentation] = useState("");
  const [subject, setSubject] = useState("");
  const [fromKey, setFromKey] = useState("");
  const [toKey, setToKey] = useState("");
  const [allowPartial, setAllowPartial] = useState(false);
  const initialized = useRef(false);
  const active = useRef<AbortController | null>(null);
  const clear = useCallback(() => {
    active.current?.abort();
    setDataset(null);
    setIssues(null);
    setPreview(null);
  }, []);
  const failure = useFailure(clear);
  const back = `${ROOT}?${new URLSearchParams([...params].filter(([key]) => ["search", "status", "page"].includes(key)))}`;

  useEffect(() => {
    const controller = new AbortController();
    setBusy(true);
    setError("");
    dataStoreApi<CurrentDataset>(`/datasets/${encodeURIComponent(datasetId)}`, controller.signal)
      .then(result => {
        if (controller.signal.aborted) return;
        setDataset(result);
        if (!initialized.current && result.preview_key) {
          setRepresentation(result.preview_key.representation);
          setSubject(result.preview_key.subject);
          setFromKey(result.preview_key.object_key);
          setToKey(result.preview_key.object_key);
          initialized.current = true;
        }
        if (preview && (result.generation !== preview.generation || result.status === "rebuilding")) {
          setPreview(null);
          setPage(0);
          setCursors([null]);
          setPreviewError("当前数据已改变，请重新读取预览。");
        }
      })
      .catch(problem => { if (!controller.signal.aborted) setError(failure(problem)); })
      .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [datasetId, refresh, failure]);

  useEffect(() => {
    if (!dataset?.issues && !dataset?.legacy_restrictions) {
      setIssues(null);
      setIssuesError("");
      return;
    }
    const controller = new AbortController();
    setIssuesError("");
    dataStoreApi<IssueList>(`/issues?dataset=${encodeURIComponent(datasetId)}&limit=20`, controller.signal)
      .then(result => { if (!controller.signal.aborted) setIssues(result); })
      .catch(problem => { if (!controller.signal.aborted) setIssuesError(failure(problem)); });
    return () => controller.abort();
  }, [dataset?.issues, dataset?.legacy_restrictions, datasetId, refresh, failure]);

  useEffect(() => () => active.current?.abort(), []);

  async function read(target: number) {
    if (!dataset || !representation || !subject || !fromKey || !toKey) return;
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    setReading(true);
    setPreviewError("");
    const columns = [...new Set([
      "representation", "subject", "object_key", "member_key", "basis_state",
      ...dataset.fields.filter(field => /^f\d+_/.test(field.column))
        .slice(0, 6).map(field => field.column)
    ])];
    const body: PreviewRequest = {
      dataset: dataset.dataset,
      frequency: dataset.frequency,
      representation, subject, from_key: fromKey, to_key: toKey,
      columns, page_size: 20, cursor: cursors[target], allow_partial: allowPartial
    };
    try {
      const result = await dataStoreApi<PreviewResult>("/query", controller.signal, body);
      if (controller.signal.aborted) return;
      setPreview(result);
      setPage(target);
      setCursors(old => target === 0
        ? [null, ...(result.next_cursor ? [result.next_cursor] : [])]
        : [...old.slice(0, target + 1), ...(result.next_cursor ? [result.next_cursor] : [])]);
    } catch (problem) {
      if (controller.signal.aborted) return;
      if (problem instanceof DataStoreApiError && ["DATA_CHANGED", "REBUILD_REQUIRED", "DATA_STORE_REBUILDING"].includes(problem.code)) {
        setPreview(null); setPage(0); setCursors([null]);
      }
      setPreviewError(failure(problem));
    } finally {
      if (!controller.signal.aborted) setReading(false);
    }
  }

  function changeScope(update: () => void) {
    active.current?.abort();
    update();
    setReading(false);
    setPreview(null);
    setPreviewError("");
    setPage(0);
    setCursors([null]);
  }

  const fields = preview?.rows[0] ? Object.keys(preview.rows[0]) : [];
  return <section className="qf-assets" aria-busy={busy}>
    <header className="qf-assets-heading"><div>
      <Link className="qf-assets-back" to={back}>← 数据资产</Link>
      <h1>{dataset?.name ?? "数据集详情"}</h1>
      <p>{dataset ? <code>{dataset.dataset}</code> : "正在读取当前数据集…"}</p>
    </div><button className="qfo-secondary-btn" type="button" disabled={busy} onClick={() => setRefresh(value => value + 1)}>
      <RefreshCw size={16} aria-hidden="true" />{busy ? "刷新中…" : "刷新"}
    </button></header>
    {error && <p role="alert" className="qf-assets-error">{error}{dataset && " 当前保留上次读取的数据集摘要及时间。"}</p>}
    {!dataset && !error && <p role="status">正在读取数据集详情…</p>}
    {dataset && <>
      {dataset.status === "rebuilding" && <div className="qf-assets-banner" role="status">
        数据底座正在维护重建。当前目录可查看，预览需等待显式重建验收完成。
      </div>}
      <div className="qf-assets-metrics">
        <div><span>当前状态</span><strong className="qf-assets-metric-label">{statusNames[dataset.status] ?? "状态未知"}</strong></div>
        <div><span>当前记录</span><strong>{count(dataset.row_count)}</strong></div>
        <div><span>当前代次</span><strong>{count(dataset.generation)}</strong></div>
        <div><span>未解决问题</span><strong>{count(dataset.issues)}</strong></div>
      </div>
      <div className="qf-assets-grid">
        <section className="qf-assets-sheet"><h2>当前数据</h2>
          <dl className="qf-assets-properties">
            <div><dt>数据来源</dt><dd>{dataset.source}</dd></div>
            <div><dt>频率</dt><dd>{frequencies[dataset.frequency] ?? "未声明"}</dd></div>
            <div><dt>存储口径</dt><dd>类型化对象节点 · {dataset.representation}</dd></div>
            <div><dt>当前分区</dt><dd>{dataset.partition_range.from ?? "未检查"}{dataset.partition_range.to && ` ～ ${dataset.partition_range.to}`}</dd></div>
            <div><dt>最近提交</dt><dd>{time(dataset.updated_at)}</dd></div>
          </dl>
          <p className="qf-assets-asof">分区范围是存储范围，不证明每个业务日期均有数据。Schema {dataset.schema_id}。</p>
          {dataset.status === "empty" && <p className="qf-assets-empty">当前数据集已登记，尚无正式记录。</p>}
          {dataset.status === "not_checked" && <p className="qf-assets-empty">本地来源尚未检查，不能将未检查视为空。</p>}
        </section>
        <section className="qf-assets-sheet"><h2>处理与限制</h2>
          {dataset.last_update ? <dl className="qf-assets-properties">
            <div><dt>最近更新</dt><dd>{statusNames[dataset.last_update.state] ?? "状态待核对"}</dd></div>
            <div><dt>来源记录</dt><dd>{count(dataset.last_update.source_rows)}</dd></div>
            <div><dt>提交分区</dt><dd>{count(dataset.last_update.committed_partitions)}</dd></div>
            <div><dt>执行时间</dt><dd>{time(dataset.last_update.updated_at)}</dd></div>
          </dl> : <p>尚无本地处理记录。</p>}
          {dataset.last_update && !dataset.last_update.complete &&
            <p className="qf-assets-warning">最近一次更新未完成；已有当前数据是否可用请以左侧状态和查询结果为准。</p>}
          {!!dataset.legacy_restrictions && <p className="qf-assets-warning">旧数据限制仍待定位或处理，相关查询不会绕过限制。</p>}
          {dataset.limitations.length > 0 && <details><summary>已知能力边界</summary>
            <ul>{dataset.limitations.map(item => <li key={item}>{item}</li>)}</ul>
          </details>}
        </section>
      </div>
      {issuesError && <p role="alert" className="qf-assets-error">问题详情读取失败：{issuesError}{issues && " 当前保留上次读取的问题列表。"}</p>}
      {(issues?.items.length ?? 0) > 0 && <section className="qf-assets-sheet">
        <h2>当前问题与限制</h2><p>显示最近 {issues?.items.length} 项；共 {issues?.total} 项。</p>
        <ul className="qf-assets-issues">{issues?.items.map((issue, index) => <li key={`${issue.kind}-${issue.scope_key}-${index}`}>
          <strong>{issueNames[issue.reason] ?? "当前数据限制"}</strong>
          <span>{issue.scope_key ?? "范围待定位"} · {time(issue.updated_at)}</span>
          <details><summary>技术原因</summary><code>{issue.reason}</code></details>
        </li>)}</ul>
      </section>}
      <section className="qf-assets-sheet"><h2>基本预览</h2>
        <p>按当前口径、标的和业务键范围读取。默认要求范围内数据无未解决限制；跨页时会检查代次。</p>
        {!dataset.preview_key && <p className="qf-assets-asof">暂无可自动填写的预览起点；可手动输入已知的口径、标的和业务键。</p>}
        <form className="qf-assets-form" onSubmit={event => { event.preventDefault(); void read(0); }}>
          <label>口径标识<input value={representation} onChange={event => changeScope(() => setRepresentation(event.target.value))} /></label>
          <label>标的标识<input value={subject} onChange={event => changeScope(() => setSubject(event.target.value))} /></label>
          <label>起始业务键<input value={fromKey} onChange={event => changeScope(() => setFromKey(event.target.value))} /></label>
          <label>结束业务键<input value={toKey} onChange={event => changeScope(() => setToKey(event.target.value))} /></label>
          <label className="qf-assets-check"><input type="checkbox" checked={allowPartial}
            onChange={event => changeScope(() => setAllowPartial(event.target.checked))} />
            明确允许部分结果及有问题的范围</label>
          <button className="qfo-primary-btn" type="submit"
            disabled={reading || dataset.status === "rebuilding" || !representation || !subject || !fromKey || !toKey}>
            {reading ? "读取中…" : "读取当前预览"}
          </button>
        </form>
        {previewError && <p role="alert" className="qf-assets-error">{previewError}{preview && " 当前保留上次获准预览。"}</p>}
        {preview && <>
          <p role="status" className="qf-assets-preview-meta">
            {preview.status === "restricted" ? "部分结果，存在当前问题" : preview.rows.length ? "当前预览" : "所选范围没有匹配记录"}
            {" · "}代次 {count(preview.generation)}{" · "}本页业务键 {preview.actual_range.from ?? "无"} ～ {preview.actual_range.to ?? "无"}
          </p>
          {preview.rows.length > 0 && <div className="qf-assets-scroll"><table><thead><tr>
            {fields.map(field => <th key={field}>{field}</th>)}
          </tr></thead><tbody>{preview.rows.map((row, index) => <tr key={index}>
            {fields.map(field => <td key={field} className={typeof row[field] === "number" ? "qf-assets-number" : undefined}>
              {row[field] == null ? "—" : String(row[field])}
            </td>)}
          </tr>)}</tbody></table></div>}
          <div className="qf-assets-pager">
            <span>第 {page + 1} 页 · 本页 {preview.rows.length} 行</span>
            <button className="qfo-secondary-btn" type="button" disabled={reading || page === 0} onClick={() => void read(page - 1)}>上一页</button>
            <button className="qfo-secondary-btn" type="button" disabled={reading || !preview.next_cursor} onClick={() => void read(page + 1)}>下一页</button>
          </div>
          {preview.partial_requested && <p className="qf-assets-warning">已明确允许部分结果；此预览不代表原范围完整可用。</p>}
        </>}
      </section>
      <section className="qf-assets-sheet"><h2>字段与口径</h2>
        <div className="qf-assets-scroll"><table><thead><tr><th>字段</th><th>类型</th><th>字段含义</th><th>计算能力</th></tr></thead>
          <tbody>{dataset.fields.map(field => <tr key={field.column}>
            <td><code>{field.column}</code></td><td>{field.type}</td>
            <td>{field.meaning}</td><td>{field.arithmetic === "unsupported" ? "不支持直接计算" : "以领域契约为准"}</td>
          </tr>)}</tbody>
        </table></div>
      </section>
    </>}
  </section>;
}

export function DataAssetsPage() {
  const { datasetId } = useParams();
  return datasetId ? <DatasetDetail key={datasetId} datasetId={datasetId} /> : <Catalog />;
}
