import { useCallback, useEffect, useRef, useState } from "react";
import { buildPreviewRequest, createPreviewSession, formatCount as count, formatTimestamp,
  isCancellation, type CurrentDataset, type PreviewRequest, type PreviewResult, type PreviewSession } from "./data";
import { DataSheet, LocalNotice } from "./components";
import { useDataAssetsFailure } from "./components/useDataAssetsFailure";

export interface PreviewPanelProps { dataset: CurrentDataset; }

/** D05 owns preview behavior; its state survives view changes for this object. */
export function PreviewPanel({ dataset }: PreviewPanelProps) {
  const [previewError, setPreviewError] = useState("");
  const [reading, setReading] = useState(false);
  const [preview, setPreview] = useState<PreviewResult | null>(null);
  const [page, setPage] = useState(0);
  const [readAt, setReadAt] = useState<string | null>(null);
  const [representation, setRepresentation] = useState(dataset.preview_key?.representation ?? "");
  const [subject, setSubject] = useState(dataset.preview_key?.subject ?? "");
  const [fromKey, setFromKey] = useState(dataset.preview_key?.object_key ?? "");
  const [toKey, setToKey] = useState(dataset.preview_key?.object_key ?? "");
  const initialized = useRef(Boolean(dataset.preview_key));
  const observedDataset = useRef({ generation: dataset.generation, status: dataset.status });
  const active = useRef<AbortController | null>(null);
  const session = useRef<PreviewSession | null>(null);
  const clear = useCallback(() => {
    active.current?.abort(); session.current?.reset();
    setReading(false); setPreview(null); setPage(0); setReadAt(null);
  }, []);
  const failure = useDataAssetsFailure(clear, setPreviewError);

  useEffect(() => {
    // Effect-owned sessions survive view changes and also tolerate StrictMode's
    // mount/cleanup replay without reusing a disposed request scope.
    const current = createPreviewSession();
    session.current = current;
    return () => { active.current?.abort(); current.dispose(); session.current = null; };
  }, []);

  useEffect(() => {
    const previous = observedDataset.current;
    observedDataset.current = { generation: dataset.generation, status: dataset.status };
    if (!initialized.current && dataset.preview_key) {
      setRepresentation(dataset.preview_key.representation); setSubject(dataset.preview_key.subject);
      setFromKey(dataset.preview_key.object_key); setToKey(dataset.preview_key.object_key);
      initialized.current = true;
    }
    const restricted = ["restricted", "rebuilding", "rebuild_required"].includes(dataset.status);
    // Refresh can cross a read boundary before the first preview completes.
    // Cancel that request as well as any already displayed rows/cursors.
    if (previous.generation !== dataset.generation || (previous.status !== dataset.status && restricted)
      || (preview && (dataset.generation !== preview.generation || restricted))) {
      clear(); setPreviewError("当前数据已改变，请重新读取预览。");
    }
  }, [dataset, clear]);

  async function read(target: number) {
    const current = session.current;
    if (!current || !representation || !subject || !fromKey || !toKey) return;
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
    try {
      const base = dataset.preview_key ? buildPreviewRequest(dataset, { columns }) : {
        dataset: dataset.dataset, frequency: dataset.frequency, columns, page_size: 20, allow_partial: false
      };
      const body: PreviewRequest = { ...base, representation, subject,
        from_key: fromKey, to_key: toKey, cursor: null };
      await current.read(body, { page: target, signal: controller.signal });
      if (controller.signal.aborted) return;
      const snapshot = current.getSnapshot();
      setPreview(snapshot.result); setPage(snapshot.page); setReadAt(snapshot.readAt);
    } catch (problem) {
      if (controller.signal.aborted || isCancellation(problem)) return;
      const snapshot = current.getSnapshot();
      setPreview(snapshot.result); setPage(snapshot.page); setReadAt(snapshot.readAt);
      setPreviewError(failure(problem));
    } finally {
      if (!controller.signal.aborted) setReading(false);
    }
  }

  function changeScope(update: () => void) {
    clear();
    update();
    setPreviewError("");
  }

  const fields = preview?.rows[0] ? Object.keys(preview.rows[0]) : [];
  return <DataSheet title="当前数据预览">
        <p>查看服务端提供的当前示例对象；范围覆盖以服务端声明为准。</p>
        {!dataset.preview_key && <p className="qf-assets-asof">当前未提供示例起点；已知条件可在技术信息中填写。</p>}
        <form onSubmit={event => { event.preventDefault(); void read(0); }}>
          <details><summary>调整预览条件（技术信息）</summary><div className="qf-assets-form">
          <label>口径标识<input value={representation} onChange={event => changeScope(() => setRepresentation(event.target.value))} /></label>
          <label>标的标识<input value={subject} onChange={event => changeScope(() => setSubject(event.target.value))} /></label>
          <label>起始业务键<input value={fromKey} onChange={event => changeScope(() => setFromKey(event.target.value))} /></label>
          <label>结束业务键<input value={toKey} onChange={event => changeScope(() => setToKey(event.target.value))} /></label>
          </div></details>
          <p className="qf-assets-asof">默认拒绝未解决限制；本页结果不证明业务日期完整覆盖。</p>
          <button className="qfo-primary-btn" type="submit"
            disabled={reading || dataset.status === "rebuilding" || !representation || !subject || !fromKey || !toKey}>
            {reading ? "读取中…" : "读取当前预览"}
          </button>
        </form>
        {previewError && <LocalNotice tone="error">{previewError}{preview && ` 当前保留 ${formatTimestamp(readAt)} 读取的预览。`}</LocalNotice>}
        {preview && <>
          <p role="status" className="qf-assets-preview-meta">
            {preview.status === "restricted" ? "部分结果，存在当前问题" : preview.rows.length ? "当前预览" : "所选范围没有匹配记录"}
            {" · "}代次 {count(preview.generation)}{" · "}本页业务键 {preview.actual_range.from ?? "无"} ～ {preview.actual_range.to ?? "无"}
          </p>
          <p className="qf-assets-asof">页面读取于 {formatTimestamp(readAt)}。</p>
          {preview.rows.length > 0 && <div className="qf-assets-scroll" tabIndex={0} role="region" aria-label="当前数据预览表格"><table><thead><tr>
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
      </DataSheet>;
}
