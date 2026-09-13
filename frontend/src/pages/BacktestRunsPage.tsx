import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { Plus, RefreshCw, Search, X } from "lucide-react";
import { cancelBacktestRun, fetchRunWorkbench, getBacktestRun, isTerminalBacktestStatus, rerunBacktest, type BacktestRun, type WorkbenchPage, type WorkbenchRun } from "../api/backtestRuns";
import { compareBacktestRuns } from "../api/backtestPreflight";
import { BacktestComparisonView } from "../components/BacktestReport";
import { CreateRunDrawer } from "./backtests/CreateRunDrawer";
import { RunResults } from "./backtests/RunResults";
import { RunPreview, dateText } from "./backtests/RunPreview";
import { copyConfiguration, STATUS } from "./backtests/workbench";
import "./backtests/Workbench.css";

export function BacktestRunsPage() {
  const { strategyId } = useParams();
  const [page, setPage] = useState<WorkbenchPage | null>(null), [selected, setSelected] = useState<WorkbenchRun | null>(null);
  const [search, setSearch] = useState(""), [query, setQuery] = useState({ search: "", status: "", offset: 0 });
  const [loading, setLoading] = useState(false), [error, setError] = useState(""), [toast, setToast] = useState("");
  const [drawer, setDrawer] = useState<{ source?: BacktestRun; strategyId?: string } | null>(null);
  const [compareMode, setCompareMode] = useState(false), [compareIds, setCompareIds] = useState<string[]>([]);
  const [result, setResult] = useState<{ kind: "report"; run: WorkbenchRun } | { kind: "compare"; value: any } | null>(null);
  const [busy, setBusy] = useState(false);
  const pageRef = useRef(page), selectedRef = useRef(selected), sequence = useRef(0), detailSequence = useRef(0), controller = useRef<AbortController | null>(null), actionBusy = useRef(false);
  const rerunKey = useRef<{ id: string; key: string } | null>(null);
  pageRef.current = page; selectedRef.current = selected;
  useEffect(() => { const timer = window.setTimeout(() => setQuery(q => q.search === search.trim() ? q : { ...q, search: search.trim(), offset: 0 }), 300); return () => clearTimeout(timer); }, [search]);
  useEffect(() => { if (!toast) return; const timer = setTimeout(() => setToast(""), 4000); return () => clearTimeout(timer); }, [toast]);
  async function refresh() {
    const generation = ++sequence.current;
    controller.current?.abort(); const request = new AbortController(); controller.current = request;
    setLoading(true); setError("");
    try {
      const fresh = await fetchRunWorkbench({ ...query, strategy_id: strategyId }, request.signal);
      if (generation !== sequence.current) return;
      if (query.offset > 0 && !fresh.items.length && fresh.total <= query.offset) { setQuery(q => ({ ...q, offset: Math.max(0, Math.ceil(fresh.total/20)*20-20) })); return; }
      setPage(fresh);
      const previous = selectedRef.current;
      const current = fresh.items.find(r => r.run_id === previous?.run_id);
      if (current) { selectedRef.current = current; setSelected(current); }
      else if (!previous && fresh.items.length) { selectedRef.current = fresh.items[0]; setSelected(fresh.items[0]); }
      else if (previous && !isTerminalBacktestStatus(previous.status)) {
        const detail = await getBacktestRun(previous.run_id, request.signal);
        if (generation === sequence.current && selectedRef.current?.run_id === previous.run_id) { const updated = { ...previous, ...detail }; selectedRef.current = updated; setSelected(updated); }
      }
    } catch (caught) { if (!request.signal.aborted) setError(caught instanceof Error ? caught.message : "运行历史加载失败。"); }
    finally { if (generation === sequence.current) setLoading(false); }
  }
  useEffect(() => {
    // Query changes select from the new result set, while manual refresh retains
    // both empty and populated content until the response is ready.
    selectedRef.current = null; setSelected(null); detailSequence.current += 1;
    void refresh();
    return () => { sequence.current += 1; controller.current?.abort(); };
  }, [query, strategyId]);
  useEffect(() => {
    let stopped = false, timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      if (stopped || document.visibilityState !== "visible") return;
      const active = pageRef.current?.items.some(r => !isTerminalBacktestStatus(r.status)) || selectedRef.current && !isTerminalBacktestStatus(selectedRef.current.status);
      if (active) await refresh();
      if (!stopped) timer = setTimeout(poll, 5000);
    };
    const visible = () => { clearTimeout(timer); if (document.visibilityState === "visible") void poll(); };
    timer = setTimeout(poll, 5000); document.addEventListener("visibilitychange", visible);
    return () => { stopped = true; clearTimeout(timer); document.removeEventListener("visibilitychange", visible); };
  }, [query, strategyId]);
  function select(run: WorkbenchRun) {
    // The workspace row already contains the full run projection. Selecting it
    // requires no second request that could overwrite a newer polling response.
    selectedRef.current = run; setSelected(run);
  }
  function toggle(id: string) { setCompareIds(ids => ids.includes(id) ? ids.filter(value => value !== id) : ids.length < 10 ? [...ids, id] : ids); }
  async function action(kind: "cancel" | "rerun") {
    if (!selected || actionBusy.current) return;
    actionBusy.current = true; setBusy(true); setError("");
    try {
      // An uncertain network response must not turn a retry into another run.
      if (kind === "rerun" && rerunKey.current?.id !== selected.run_id) rerunKey.current = { id: selected.run_id, key: crypto.randomUUID() };
      const fresh = kind === "cancel" ? await cancelBacktestRun(selected.run_id) : await rerunBacktest(selected, rerunKey.current!.key);
      if (kind === "rerun") rerunKey.current = null;
      if (selectedRef.current?.run_id === selected.run_id) {
        const updated = { ...selected, ...(fresh as BacktestRun) }; selectedRef.current = updated; setSelected(updated);
      }
      setToast(kind === "cancel" ? "取消请求已提交。" : "已按原冻结配置加入回测队列。");
      await refresh();
    } catch (caught) { setError(caught instanceof Error ? caught.message : "操作失败。"); }
    finally { actionBusy.current = false; setBusy(false); }
  }
  async function compare() {
    if (actionBusy.current || compareIds.length < 2) return;
    actionBusy.current = true; setBusy(true); setError("");
    try { setResult({ kind: "compare", value: await compareBacktestRuns(compareIds) }); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "对比加载失败。"); }
    finally { actionBusy.current = false; setBusy(false); }
  }
  function copy() {
    if (!selected) return;
    try { copyConfiguration(selected); if (!selected.strategy_id) throw new Error("无法定位原策略，请创建新的回测。"); setDrawer({ source: selected, strategyId: selected.strategy_id }); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "无法复制配置。"); }
  }
  return <div className="qfb-page" data-polling-protocol="foreground_polling@1">
    <div className="qfb-heading"><div><div className="qfb-eyebrow">BACKTEST WORKBENCH</div><h1>回测工作台</h1><p>创建回测、跟踪运行，并查看真实策略表现。</p></div><div className="qfb-actions">
      <button onClick={() => void refresh()} disabled={loading}><RefreshCw size={16} />刷新</button>
      <button onClick={() => setCompareMode(value => !value)}>{compareMode ? "退出选择" : "选择对比"}</button>
      <button className="qfb-primary" onClick={() => setDrawer({ strategyId })}><Plus size={16} />创建回测</button>
    </div></div>
    {toast && <div className="qfb-toast" role="status">{toast}<button aria-label="关闭提示" onClick={() => setToast("")}><X size={16}/></button></div>}
    {error && <div className="qfb-error" role="alert">{error}<button aria-label="关闭错误" onClick={() => setError("")}><X size={16}/></button></div>}
    {result ? <section className="qfb-results"><button onClick={() => setResult(null)}>返回回测工作台</button><h2>{result.kind === "report" ? "完整回测结果" : "回测对比"}</h2>{result.kind === "report" ? <RunResults key={result.run.run_id} run={selected?.run_id === result.run.run_id ? selected : result.run} /> : <BacktestComparisonView result={result.value} />}</section> : <div className="qfb-workspace">
      <aside className="qfb-history"><header><h2>运行历史</h2><span>{page ? `${page.total} 次运行` : "—"}</span></header>
        <div className="qfb-filters"><label className="qfb-search"><Search size={15}/><input aria-label="搜索运行 ID 或策略" placeholder="搜索运行 ID / 策略" value={search} maxLength={200} onChange={e => setSearch(e.target.value)} /></label><select aria-label="运行状态" value={query.status} onChange={e => setQuery(q => ({ ...q, status: e.target.value, offset: 0 }))}><option value="">全部状态</option>{Object.entries(STATUS).map(([key,label]) => <option value={key} key={key}>{label}</option>)}</select></div>
        <div className="qfb-history-scroll">{!page ? <div className="qfb-empty">{error ? "运行历史加载失败" : "正在加载运行历史…"}</div> : !page.items.length ? <div className="qfb-empty"><strong>{query.search || query.status ? "没有匹配的运行" : "还没有回测记录"}</strong><p>{query.search || query.status ? "调整搜索或筛选条件。" : "创建一次回测，开始查看策略表现。"}</p></div> : page.items.map(run => <div className={`qfb-run-row ${selected?.run_id === run.run_id ? "is-selected" : ""}`} key={run.run_id}>
          {compareMode && <input type="checkbox" aria-label={`对比 ${run.run_id}`} checked={compareIds.includes(run.run_id)} disabled={run.status !== "succeeded" || !compareIds.includes(run.run_id) && compareIds.length >= 10} onChange={() => toggle(run.run_id)} />}
          <button onClick={() => void select(run)} aria-pressed={selected?.run_id === run.run_id}><div><strong>{run.strategy_name || "策略信息未提供"}</strong><span className={`qfb-badge qfb-${run.status}`}>{STATUS[run.status]}</span></div><code>{run.run_id.slice(0, 8)} · {run.revision_number ? `版本 ${run.revision_number}` : "历史记录"}</code><small>{dateText(run.created_at)}</small></button>
        </div>)}</div>
        <footer><span>{page?.total ? `${query.offset+1}–${query.offset+page.items.length} / ${page.total}` : "0 / 0"}</span><button disabled={loading || query.offset === 0} onClick={() => setQuery(q => ({ ...q, offset: Math.max(0,q.offset-20) }))}>上一页</button><button disabled={loading || !page?.has_more} onClick={() => setQuery(q => ({ ...q, offset: q.offset+20 }))}>下一页</button></footer>
      </aside>
      <main className="qfb-detail">{selected ? <><header className="qfb-detail-head"><div><div className="qfb-eyebrow">BACKTEST RUN</div><h2>{selected.strategy_name || "回测运行"}</h2><code>{selected.run_id}</code></div><div className="qfb-actions"><button onClick={() => setResult({ kind: "report", run: selected })}>完整结果</button><button disabled={selected.status !== "succeeded" || !compareIds.includes(selected.run_id) && compareIds.length >= 10} onClick={() => { toggle(selected.run_id); setCompareMode(true); }}>{compareIds.includes(selected.run_id) ? "移出对比" : "加入对比"}</button><button onClick={copy}>复制配置</button>{isTerminalBacktestStatus(selected.status) ? <button disabled={busy} onClick={() => void action("rerun")}>重新运行</button> : <button disabled={busy || selected.status === "cancel_requested"} onClick={() => void action("cancel")}>{selected.status === "cancel_requested" ? "取消处理中" : "取消运行"}</button>}</div></header><div className="qfb-preview"><RunPreview run={selected} /></div></> : <div className="qfb-empty">{page ? "选择运行查看结果" : "正在加载回测工作台…"}</div>}</main>
    </div>}
    {compareMode && !result && <div className="qfb-compare-bar"><span>已选择 {compareIds.length} / 10 个运行</span><small>选择 2–10 个已完成运行进行对比</small><button disabled={!compareIds.length} onClick={() => setCompareIds([])}>清空</button><button className="qfb-primary" disabled={busy || compareIds.length < 2} onClick={() => void compare()}>开始对比</button></div>}
    {drawer && <CreateRunDrawer initialStrategyId={drawer.strategyId} source={drawer.source} onClose={() => setDrawer(null)} onCreated={run => {
      setDrawer(null); setToast("回测已创建，已加入运行队列。");
      const selectedRun = { ...run, strategy_id: drawer.strategyId || null, strategy_name: null, revision_number: null };
      selectedRef.current = selectedRun; setSelected(selectedRun); setQuery({ search: "", status: "", offset: 0 }); setSearch("");
    }} />}
  </div>;
}
