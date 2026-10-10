import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { createRequestScope, currentStatus, dataAssetsClient, frequencyLabel, isCancellation,
  type CurrentDataset, type RequestScope } from "./data";
import { DatasetView } from "./DatasetView";
import { PreviewPanel } from "./PreviewPanel";
import { UpdateIssuesPanel } from "./UpdateIssuesPanel";
import { DatasetTabs, LocalNotice, WorkspaceHeader } from "./components";
import { useDataAssetsFailure } from "./components/useDataAssetsFailure";
import { catalogHref, dataAssetsView, viewParams, type DataAssetsView } from "./navigation";

export interface DataAssetsLayoutProps { datasetId: string; }

/** Own detail navigation and the shared object, leaving each content module isolated. */
export function DataAssetsLayout({ datasetId }: DataAssetsLayoutProps) {
  const [params, setParams] = useSearchParams();
  const view = dataAssetsView(params.get("view"));
  const [dataset, setDataset] = useState<CurrentDataset | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const active = useRef<RequestScope | null>(null);
  const scroll = useRef<Record<DataAssetsView, number>>({ overview: 0, preview: 0, updates: 0 });
  const clear = useCallback(() => {
    active.current?.cancel(); setDataset(null); setBusy(false);
  }, []);
  const failure = useDataAssetsFailure(clear, setError);

  useEffect(() => {
    const scope = createRequestScope();
    active.current = scope;
    let disposed = false;
    setBusy(true); setError("");
    scope.run(signal => dataAssetsClient.getDataset(datasetId, { signal }))
      .then(result => { if (!disposed) setDataset(result); })
      .catch(problem => { if (!disposed && !isCancellation(problem)) setError(failure(problem)); })
      .finally(() => { if (!disposed && active.current === scope) setBusy(false); });
    return () => { disposed = true; scope.dispose(); };
  }, [datasetId, refreshVersion, failure]);

  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      const main = document.querySelector(".qfo-main");
      if (main) main.scrollTop = scroll.current[view];
    });
    return () => cancelAnimationFrame(frame);
  }, [view]);

  function changeView(next: DataAssetsView) {
    if (next === view) return;
    scroll.current[view] = document.querySelector(".qfo-main")?.scrollTop ?? 0;
    setParams(viewParams(params, next));
  }

  return <section className="qf-assets" aria-busy={busy}>
    <WorkspaceHeader title={dataset?.name ?? "数据集详情"}
      description={dataset ? `${frequencyLabel(dataset.frequency)} · ${dataset.source}` : "查看当前数据集、字段和能力说明。"}
      back={<Link className="qf-assets-back" to={catalogHref(params)}>← 数据资产</Link>}
      status={dataset && <span className={`qf-assets-state state-${dataset.status}`}>{currentStatus(dataset.status).label}</span>}
      actions={<>
        <button className="qfo-secondary-btn" type="button" disabled={busy} onClick={() => setRefreshVersion(value => value + 1)}>
          <RefreshCw size={16} aria-hidden="true" />{busy ? "刷新中…" : "刷新页面"}
        </button>
        {view !== "preview" && <button className="qfo-primary-btn" type="button" disabled={!dataset} onClick={() => changeView("preview")}>
          查看当前数据
        </button>}
      </>} />
    {error && <LocalNotice tone="error">{error}{dataset && " 当前保留上次读取的数据集摘要。"}</LocalNotice>}
    {dataset?.status === "rebuilding" && <LocalNotice tone="warning">当前数据暂不可读取，请稍后刷新页面。</LocalNotice>}
    <DatasetTabs value={view} onChange={changeView} />
    {/* Keep same-object panels mounted so tab navigation preserves local inputs,
        issues and preview cursors. The keyed route unmounts all on object change. */}
    {(["overview", "preview", "updates"] as const).map(panel => <div key={panel}
      id={`qf-assets-panel-${panel}`} role="tabpanel" aria-labelledby={`qf-assets-tab-${panel}`}
      tabIndex={0} hidden={view !== panel} className="qf-assets-panel">
      {dataset ? panel === "overview" ? <DatasetView dataset={dataset} />
        : panel === "preview" ? <PreviewPanel dataset={dataset} />
        : <UpdateIssuesPanel dataset={dataset} refreshVersion={refreshVersion} />
        : !error && <div className="qf-assets-loading" role="status">正在读取数据集详情…</div>}
    </div>)}
  </section>;
}
