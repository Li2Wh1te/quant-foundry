import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useLocation, useSearchParams } from "react-router-dom";
import { buildPreviewRequest, createPreviewSession, DataStoreApiError, formatCount,
  formatTimestamp, formatValue, isCancellation, type CurrentDataset, type CurrentValue,
  type PreviewResult, type PreviewSession } from "./data";
import { DataSheet, LocalNotice } from "./components";
import { useDataAssetsFailure } from "./components/useDataAssetsFailure";
import "./PreviewPanel.css";

export interface PreviewPanelProps { dataset: CurrentDataset; }
const INTERNAL_COLUMNS = new Set(["representation", "subject", "object_key", "member_key",
  "row_kind", "basis_ns", "basis_group", "basis_token", "basis_state", "quality_json"]);
// D02 may unmount every panel on an HTTP denial and remount on a GET refresh.
// Remember only the last consumed navigation action, never rows, tokens or cursors.
let consumedActivation: string | null = null;

/** Only declared columns are selectable. Identity keys also come from the descriptor. */
export function previewColumnOptions(dataset: CurrentDataset): string[] {
  return [...new Set([...dataset.business_key, ...dataset.fields.map(field => field.column)])];
}
export function previewIdentityColumns(dataset: CurrentDataset): string[] {
  const declared = previewColumnOptions(dataset);
  return ["object_key", "member_key"].filter(column => declared.includes(column));
}
export function defaultPreviewColumns(dataset: CurrentDataset): string[] {
  return [...previewIdentityColumns(dataset), ...dataset.fields.map(field => field.column)
    .filter(column => !INTERNAL_COLUMNS.has(column))].filter((column, index, all) => all.indexOf(column) === index)
    .slice(0, 8);
}
/** A schema/key change invalidates conditions; read boundaries invalidate results separately. */
export function previewScopeKey(dataset: CurrentDataset): string {
  return JSON.stringify([dataset.dataset, dataset.frequency, dataset.schema_id, dataset.rule,
    dataset.preview_key, dataset.business_key, dataset.fields]);
}
export function previewCellValue(value: CurrentValue | undefined): string {
  return value == null ? "—" : formatValue(value);
}
export function previewEmptyMessage(dataset: CurrentDataset, result: PreviewResult): string {
  if (dataset.status === "empty") return "数据集当前为空（按服务端声明）。";
  return result.next_cursor ? "本页为空，服务端仍提供后续页。" : "查询范围无匹配记录；不能据此判断整个数据集为空。";
}
/** Only a connection failure/timeout may retain an explicitly stale page, without cursors. */
export function retainPreviewOnFailure(problem: unknown): boolean {
  return problem instanceof DataStoreApiError && ![401, 403].includes(problem.status)
    && (problem.code === "NETWORK_ERROR" || problem.code === "QUERY_TIMEOUT");
}

function initialConditions(dataset: CurrentDataset) {
  return { subject: dataset.preview_key?.subject ?? "", fromKey: dataset.preview_key?.object_key ?? "",
    toKey: dataset.preview_key?.object_key ?? "", columns: defaultPreviewColumns(dataset), pageSize: 20 };
}

/** D05 owns one bounded, in-memory example session. The server owns read permission. */
export function PreviewPanel({ dataset }: PreviewPanelProps) {
  const id = useId();
  const location = useLocation();
  const [params] = useSearchParams();
  const visible = params.get("view") === "preview";
  const activation = JSON.stringify([location.key, dataset.dataset]);
  const scopeKey = previewScopeKey(dataset);
  const boundary = JSON.stringify([dataset.generation, dataset.status, dataset.issues, dataset.legacy_restrictions]);
  const [conditions, setConditions] = useState(() => initialConditions(dataset));
  const [previewError, setPreviewError] = useState("");
  const [notice, setNotice] = useState("");
  const [reading, setReading] = useState(false);
  const [preview, setPreview] = useState<PreviewResult | null>(null);
  const [page, setPage] = useState(0);
  const [readAt, setReadAt] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const observed = useRef({ scopeKey, boundary });
  const attempted = useRef(consumedActivation === activation);
  const active = useRef<AbortController | null>(null);
  const session = useRef<PreviewSession | null>(null);
  const clear = useCallback(() => {
    active.current?.abort(); active.current = null; session.current?.reset();
    setReading(false); setPreview(null); setPage(0); setReadAt(null); setStale(false); setNotice("");
  }, []);
  const failure = useDataAssetsFailure(clear, setPreviewError);

  useEffect(() => {
    const current = createPreviewSession();
    session.current = current;
    return () => { active.current?.abort(); active.current = null; current.dispose(); session.current = null; };
  }, [dataset.dataset]);

  useEffect(() => {
    const previous = observed.current;
    observed.current = { scopeKey, boundary };
    if (previous.scopeKey !== scopeKey || previous.boundary !== boundary) {
      clear();
      if (previous.scopeKey !== scopeKey) setConditions(initialConditions(dataset));
      // A metadata refresh never triggers another POST, even when it adds a preview key.
      if (attempted.current) setPreviewError("当前对象、字段或读取状态已改变，请重新查看当前数据。旧结果与分页已清除。");
    }
  }, [dataset, scopeKey, boundary, clear]);

  const ready = !!dataset.preview_key && dataset.frequency !== "unknown" && conditions.columns.length > 0
    && !!conditions.subject.trim() && !!conditions.fromKey.trim() && !!conditions.toKey.trim();
  const sameScope = observed.current.scopeKey === scopeKey && observed.current.boundary === boundary;
  const shown = sameScope ? preview : null;
  const busy = sameScope && reading;

  const read = useCallback(async (target: number) => {
    const current = session.current;
    if (!current || !ready || !visible || active.current) return;
    attempted.current = true;
    consumedActivation = activation;
    const controller = new AbortController();
    active.current = controller;
    setReading(true); setPreviewError(""); setNotice("");
    try {
      const body = { ...buildPreviewRequest(dataset, { columns: conditions.columns, pageSize: conditions.pageSize }),
        subject: conditions.subject, from_key: conditions.fromKey, to_key: conditions.toKey };
      const result = await current.read(body, { page: target, signal: controller.signal });
      if (controller.signal.aborted || active.current !== controller) return;
      // Unknown quality/schema is not permission to keep showing a newly returned page.
      if (!["available", "empty"].includes(result.status) || (result.schema_id && result.schema_id !== dataset.schema_id)) {
        throw new DataStoreApiError(502, "INVALID_RESPONSE", "数据服务返回格式异常，请稍后重试。");
      }
      const snapshot = current.getSnapshot();
      setPreview(snapshot.result); setPage(snapshot.page); setReadAt(snapshot.readAt); setStale(false);
    } catch (problem) {
      if (controller.signal.aborted || active.current !== controller || isCancellation(problem)) return;
      const message = failure(problem);
      const snapshot = current.getSnapshot();
      const retained = retainPreviewOnFailure(problem) ? snapshot.result ?? preview : null;
      // Retained rows are display-only. No cursor can continue from an uncertain read.
      current.reset();
      setPreview(retained); setReadAt(retained ? snapshot.readAt ?? readAt : null);
      setPage(retained ? snapshot.result ? snapshot.page : page : 0); setStale(!!retained);
      setPreviewError(message);
    } finally {
      if (active.current === controller) { active.current = null; setReading(false); }
    }
  }, [activation, conditions, dataset, failure, page, preview, readAt, ready, visible]);

  useEffect(() => {
    if (!visible) {
      if (active.current) { clear(); setNotice("已取消离开预览前的读取，请重新查看当前数据。"); }
      return;
    }
    // Defer the first request past StrictMode's effect replay. The D02 header/tab
    // is the single user action; subsequent tab visits and GET refreshes do not reread.
    let cancelled = false;
    queueMicrotask(() => {
      if (cancelled || attempted.current) return;
      attempted.current = true;
      consumedActivation = activation;
      if (ready) void read(0);
    });
    return () => { cancelled = true; };
  }, [activation, visible, ready, read, clear]);

  function changeConditions(next: Partial<typeof conditions>) {
    attempted.current = true; clear(); setPreviewError("");
    setConditions(previous => ({ ...previous, ...next }));
    setNotice("查询条件已改变，旧结果与分页已清除。请重新查看当前数据。");
  }
  const options = previewColumnOptions(dataset);
  const identities = previewIdentityColumns(dataset);
  const technical = conditions.columns.some(column => {
    if (identities.includes(column)) return false;
    const meaning = dataset.fields.find(field => field.column === column)?.meaning.trim() ?? "";
    return !meaning || /^[\w.[\]\/-]+$/.test(meaning) || INTERNAL_COLUMNS.has(column);
  }) || conditions.columns.every(column => identities.includes(column));

  return <DataSheet title="当前对象示例" className="qf-preview">
    <p>查看服务端提供的示例对象。对象键保持原始文本；范围与读取许可由服务端判断。</p>
    {!dataset.preview_key && <LocalNotice>暂无可预填对象。服务端尚未提供示例起点，请稍后刷新页面。</LocalNotice>}
    {dataset.preview_key && !options.length && <LocalNotice>服务端未声明可查询字段，暂无法构造有界预览。</LocalNotice>}
    {dataset.status === "empty" && <p>数据集当前为空（按服务端声明）；页面不会伪造示例对象。</p>}
    {dataset.status === "not_checked" && <p>数据集尚未检查，不能将它视为空。读取以服务端响应为准。</p>}
    {dataset.status === "restricted" && <p>数据集存在限制；当前对象是否可读以本次服务端判断为准。</p>}
    {dataset.status === "rebuild_required" && <p>当前数据需要重建，页面不会执行重建。</p>}
    <form onSubmit={event => { event.preventDefault(); void read(0); }} aria-busy={busy}>
      {dataset.preview_key && <p className="qf-preview-object">示例对象 <code>{conditions.subject}</code></p>}
      <div className="qf-preview-actions">
        <button className="qfo-primary-btn" type="submit" disabled={busy || !ready}>
          {busy ? "读取中…" : "查看当前数据"}
        </button>
        {busy && <button className="qfo-secondary-btn" type="button" onClick={() => {
          clear(); setPreviewError(""); setNotice("本次读取已取消，请重新查看当前数据。");
        }}>取消读取</button>}
        <span>每页最多 {conditions.pageSize} 行 · {conditions.columns.length} 列</span>
      </div>
      <details className="qf-preview-adjust"><summary>调整查询</summary>
        <p>仅调整本示例查询；不代表已支持任意标的搜索。业务键的日期语义未声明。</p>
        <div className="qf-assets-form">
          <label htmlFor={`${id}-subject`}>对象键（文本）<input id={`${id}-subject`} type="text" maxLength={256}
            value={conditions.subject} onChange={event => changeConditions({ subject: event.target.value })} /></label>
          <label htmlFor={`${id}-size`}>每页行数<select id={`${id}-size`} value={conditions.pageSize}
            onChange={event => changeConditions({ pageSize: Number(event.target.value) })}>
            {[10, 20, 50, 100].map(size => <option key={size} value={size}>{size} 行</option>)}
          </select></label>
          <label htmlFor={`${id}-from`}>起始业务键（文本）<input id={`${id}-from`} type="text" maxLength={256}
            value={conditions.fromKey} onChange={event => changeConditions({ fromKey: event.target.value })} /></label>
          <label htmlFor={`${id}-to`}>结束业务键（文本）<input id={`${id}-to`} type="text" maxLength={256}
            value={conditions.toKey} onChange={event => changeConditions({ toKey: event.target.value })} /></label>
        </div>
        <details><summary>选择预览列（{conditions.columns.length} / 32）</summary>
          <p>字段保留服务端技术名，业务单位与计算口径不从名称推断。身份列用于定位本页。</p>
          <fieldset className="qf-preview-columns"><legend>已声明列</legend>
            {options.map(column => <label key={column}>
              <input type="checkbox" checked={conditions.columns.includes(column)}
                disabled={identities.includes(column) || (!conditions.columns.includes(column) && conditions.columns.length >= 32)}
                onChange={event => changeConditions({ columns: event.target.checked ? [...conditions.columns, column]
                  : conditions.columns.filter(item => item !== column) })} />
              <span>{column}{identities.includes(column) && "（身份）"}</span>
            </label>)}
          </fieldset>
        </details>
      </details>
    </form>
    {notice && <LocalNotice>{notice}</LocalNotice>}
    {previewError && <LocalNotice tone="error">{previewError}</LocalNotice>}
    {busy && <p role="status">正在读取本页，等待服务端判断读取许可…</p>}
    {shown && <div className="qf-preview-result" aria-busy={busy}>
      <p role="status" className="qf-assets-preview-meta">{stale || busy ? "上次读取的预览" : "当前对象示例"}
        {technical && " · 技术预览"}{" · "}第 {page + 1} 页 · 本页 {shown.rows.length} 行</p>
      <p>本页范围：{shown.actual_range.from ?? "未声明"} ～ {shown.actual_range.to ?? "未声明"}</p>
      <p className="qf-assets-asof">页面读取于 {formatTimestamp(readAt)}。</p>
      {stale && <LocalNotice tone="warning">保留的是上述时刻读取的旧预览，当前读取未验证。分页已停用，请重新查看当前数据。</LocalNotice>}
      {(!shown.request_satisfied || !shown.business_date_coverage_verified) && <p className="qf-preview-note">
        {!shown.request_satisfied && "服务端未确认请求范围已满足。"}
        {!shown.business_date_coverage_verified && "业务日期覆盖未验证。"}
      </p>}
      {dataset.frequency === "report" && <LocalNotice>当前报告片段；节点可能跨页，此页不代表完整报告或持仓报告。本页行数不是对象数。</LocalNotice>}
      {!shown.rows.length ? <p className="qf-preview-empty">{previewEmptyMessage(dataset, shown)}</p>
        : <div className="qf-assets-scroll qf-preview-scroll" tabIndex={0} role="region" aria-label="当前对象示例表格">
          <table><thead><tr>{conditions.columns.map(column => <th scope="col" key={column}>{column}</th>)}</tr></thead>
            <tbody>{shown.rows.map((row, index) => <tr key={index}>{conditions.columns.map(column => <td key={column}>
              {previewCellValue(row[column])}
            </td>)}</tr>)}</tbody></table>
        </div>}
      <div className="qf-assets-pager" aria-label="当前对象预览分页">
        <span>第 {page + 1} 页 · 本页 {shown.rows.length} 行</span>
        <button className="qfo-secondary-btn" type="button" disabled={busy || stale || page === 0} onClick={() => void read(page - 1)}>上一页</button>
        <button className="qfo-secondary-btn" type="button" disabled={busy || stale || !shown.next_cursor} onClick={() => void read(page + 1)}>下一页</button>
      </div>
      <details className="qf-preview-technical"><summary>本次读取技术信息</summary>
        <p>读取一致性代次：{formatCount(shown.generation)}。它只约束当前分页读取的一致性。</p>
        <p>本页范围与页行数不能换算成完整业务覆盖或对象数量。</p>
        <p>服务端限制：{shown.limitations.length ? shown.limitations.join("；") : "未提供具体说明"}</p>
      </details>
    </div>}
    <details className="qf-preview-technical"><summary>查询技术信息</summary>
      <p>来源表示：<code>{dataset.preview_key?.representation ?? "未声明"}</code></p>
      <p>字段契约：<code>{dataset.schema_id}</code></p>
      <p>普通查询固定 allow_partial=false；此处不提供受限内容诊断读取。</p>
      {technical && <p>业务列未完整声明含义；当前为有界技术预览。Decimal、大整数和纳秒文本保持原始精度。</p>}
    </details>
  </DataSheet>;
}
