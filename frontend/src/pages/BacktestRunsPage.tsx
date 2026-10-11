import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { Plus, RefreshCw } from "lucide-react";
import { useAuth } from "../auth/AuthContext";
import * as api from "../api/runService";
import { CreateRunDrawer } from "./backtests/CreateRunDrawer";
import { STATUS, modelName, terminal } from "./backtests/run-ui/config";
import "./backtests/Workbench.css";
import "./backtests/run-ui/RunUI.css";

export function BacktestRunsPage() {
  const { strategyId } = useParams(), location = useLocation(), navigate = useNavigate(), { logout } = useAuth();
  const query = new URLSearchParams(location.search), cursor = query.get("cursor") || "", runId = query.get("run") || "", batchId = query.get("batch") || "", batchCursor = query.get("batchCursor") || "";
  const [page, setPage] = useState<api.RunPage | null>(null), [selected, setSelected] = useState<api.RunSummary | null>(null), [batch, setBatch] = useState<api.BatchPage | null>(null);
  const [loading, setLoading] = useState(false), [detailLoading, setDetailLoading] = useState(false), [batchLoading, setBatchLoading] = useState(false), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [refresh, setRefresh] = useState(0), [denied, setDenied] = useState(false), [busy, setBusy] = useState(false), [pending, setPending] = useState(false);
  const [drawer, setDrawer] = useState<{ source?: api.RunSummary } | null>(null);
  const [summary, setSummary] = useState<api.RunResultSummary | null>(null);
  const listSequence = useRef(0), detailSequence = useRef(0), batchSequence = useRef(0), action = useRef(false), mounted = useRef(true), revoked = useRef(false);
  const actionController = useRef<AbortController | null>(null);
  const drawerPath = useRef(location.pathname);
  const latest = useRef({ page, selected, batch, denied, loading, detailLoading, batchLoading }); latest.current = { page, selected, batch, denied, loading, detailLoading, batchLoading };
  useEffect(() => {
    mounted.current = true; revoked.current = false;
    const unsubscribe = api.onRunAccessDenied(status => {
      revoked.current = true; listSequence.current++; detailSequence.current++; batchSequence.current++; actionController.current?.abort();
      setPage(null); setSelected(null); setBatch(null); setSummary(null); setDrawer(null); setDenied(true);
      setError(status === 401 ? "登录状态已失效，请重新登录。" : "没有回测操作权限，请联系管理员后重试。");
      if (status === 401) { logout(); navigate("/login", { replace: true }); }
    });
    return () => { mounted.current = false; listSequence.current++; detailSequence.current++; batchSequence.current++; actionController.current?.abort(); unsubscribe(); };
  }, [logout, navigate]);
  useEffect(() => {
    let active = true; void api.pendingSubmission().then(p => { if (active) setPending(!!p); }); return () => { active = false; };
  }, [refresh, drawer]);
  useEffect(() => {
    if (denied) return;
    const c = new AbortController(), seq = ++listSequence.current; setLoading(true); setError("");
    api.listRuns(cursor || undefined, c.signal, strategyId).then(value => { if (!c.signal.aborted && seq === listSequence.current) setPage(value); })
      .catch(e => { if (!c.signal.aborted && seq === listSequence.current) setError(e.message); })
      .finally(() => { if (!c.signal.aborted && seq === listSequence.current) setLoading(false); });
    return () => c.abort();
  }, [strategyId, cursor, refresh, denied]);
  useEffect(() => { setPage(null); }, [strategyId, cursor]);
  useEffect(() => { setSelected(null); setSummary(null); }, [runId]);
  useEffect(() => { if (drawerPath.current !== location.pathname) setDrawer(null); drawerPath.current = location.pathname; }, [location.pathname]);
  useEffect(() => { setBatch(null); }, [batchId, batchCursor]);
  useEffect(() => {
    if (query.get("create") !== "1") return;
    setDrawer({}); const next = new URLSearchParams(location.search); next.delete("create");
    navigate({ pathname: location.pathname, search: next.toString() }, { replace: true });
  }, [location.pathname, location.search]);
  useEffect(() => {
    const c = new AbortController(), seq = ++detailSequence.current;
    if (!runId || denied) { setDetailLoading(false); return () => c.abort(); }
    setDetailLoading(true);
    api.getRun(runId, c.signal).then(async value => {
      if (c.signal.aborted || seq !== detailSequence.current) return; setSelected(value);
      if (terminal(value.status) && value.engine_kind === "rust") {
        const result = await api.resultSummary(runId, c.signal);
        if (!c.signal.aborted && seq === detailSequence.current) setSummary(result);
      }
    }).catch(e => { if (!c.signal.aborted && seq === detailSequence.current) setError(e.message); })
      .finally(() => { if (!c.signal.aborted && seq === detailSequence.current) setDetailLoading(false); });
    return () => c.abort();
  }, [runId, refresh, denied]);
  useEffect(() => {
    const c = new AbortController(), seq = ++batchSequence.current;
    setBatchLoading(!!batchId && !denied);
    if (batchId && !denied) api.getBatch(batchId, batchCursor || undefined, c.signal).then(value => { if (!c.signal.aborted && seq === batchSequence.current) setBatch(value); })
      .catch(e => { if (!c.signal.aborted && seq === batchSequence.current) setError(e.message); })
      .finally(() => { if (!c.signal.aborted && seq === batchSequence.current) setBatchLoading(false); });
    return () => c.abort();
  }, [batchId, batchCursor, refresh, denied]);
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    const poll = () => {
      const value = latest.current;
      if (document.visibilityState === "visible" && !value.denied && !value.loading && !value.detailLoading && !value.batchLoading && [value.selected, ...(value.page?.items || []), ...(value.batch?.items || [])].some(r => r && r.engine_kind === "rust" && !terminal(r.status))) setRefresh(v => v + 1);
      timer = setTimeout(poll, 2000);
    };
    const visible = () => { if (document.visibilityState === "visible" && !latest.current.denied) setRefresh(v => v + 1); };
    timer = setTimeout(poll, 2000); document.addEventListener("visibilitychange", visible);
    return () => { clearTimeout(timer); document.removeEventListener("visibilitychange", visible); };
  }, []);
  function route(values: Record<string, string | null>) {
    const next = new URLSearchParams(location.search); for (const [key, value] of Object.entries(values)) value ? next.set(key, value) : next.delete(key);
    navigate({ pathname: location.pathname, search: next.toString() });
  }
  function accepted(value: api.SubmissionResult) {
    if ("batch_id" in value && !('run_id' in value)) route({ batch: value.batch_id, batchCursor: null, run: "run_ids" in value ? value.run_ids[0] || null : null, cursor: null });
    else if ("run_id" in value) route({ run: value.run_id, cursor: null });
    setDrawer(null); setPending(false); setNotice("请求已受理，进度与终态以服务返回为准。"); setRefresh(v => v + 1);
  }
  async function perform(kind: "cancel" | "cancelBatch" | "retryBatch" | "retryPending") {
    if (action.current || denied) return;
    action.current = true; setBusy(true); setError(""); const c = new AbortController(); actionController.current = c;
    const targetRun = runId, targetBatch = batchId;
    try {
      if (kind === "retryPending") { const value = await api.retryPending(); if (mounted.current && !revoked.current) accepted(value); }
      else if (kind === "retryBatch") { const value = await api.retryFailed(targetBatch); if (mounted.current && !revoked.current) accepted(value); }
      else {
        if (kind === "cancel") await api.cancelRun(targetRun, c.signal); else await api.cancelBatch(targetBatch, c.signal);
        if (mounted.current && !revoked.current) { setNotice("取消请求已提交；已完成结果保留，最终状态由服务确定。"); setRefresh(v => v + 1); }
      }
    } catch(e) { if (mounted.current && !revoked.current && !c.signal.aborted) setError(e instanceof Error ? e.message : "操作失败。"); }
    finally { action.current = false; if (mounted.current) { setBusy(false); void api.pendingSubmission().then(p => { if (mounted.current) setPending(!!p); }); } }
  }
  const config = selected?.normalized_config;
  return <div className="qfb-page" data-run-ui="qf.backtest.v2">
    <div className="qfb-heading"><div><div className="qfb-eyebrow">BACKTEST WORKBENCH</div><h1>回测工作台</h1><p>创建 Rust 回测，跟踪队列与运行。数据及权限由服务核验。</p></div><div className="qfb-actions"><button disabled={loading || busy} onClick={() => { revoked.current = false; setDenied(false); setRefresh(v => v + 1); }}><RefreshCw size={16}/>刷新</button><button className="qfb-primary" disabled={denied || pending || busy} onClick={() => setDrawer({})}><Plus size={16}/>创建回测</button></div></div>
    {error && <p className="qfb-error" role="alert">{error}</p>}{notice && <p className="qfb-note" role="status">{notice}</p>}
    {pending && !denied && <div className="qfb-note" role="status">原提交结果待确认。请用原提交标识核对，确认之前暂停新建运行。<button disabled={busy} onClick={() => void perform("retryPending")}>核对原提交</button></div>}
    <div className="qfb-workspace"><aside className="qfb-history"><header><h2>运行历史</h2><small>{loading ? "刷新中…" : "每页最多 20 项"}</small></header><p className="qfb-note">旧引擎记录标为历史只读。选择运行后可用浏览器返回与前进恢复。</p><div className="qfb-history-scroll">
      {!page ? <p className="qfb-empty">{denied ? "没有访问权限" : loading ? "正在加载运行…" : "加载失败，请刷新重试。"}</p> : !page.items.length ? <p className="qfb-empty">尚无回测记录</p> : page.items.map(row => <div key={row.run_id} className={`qfb-run-row ${runId === row.run_id ? "is-selected" : ""}`}><button aria-pressed={runId === row.run_id} onClick={() => route({ run: row.run_id })}><div><strong>{row.engine_kind === "rust" ? "Rust 回测" : "历史只读"}</strong><span className={`qfb-badge qfb-${row.status}`}>{row.cancel_requested && !terminal(row.status) ? "取消处理中" : STATUS[row.status] || "未知状态"}</span></div><code>{row.run_id}</code><small>{row.created_at || "创建时间未提供"}</small></button></div>)}
    </div><footer><button disabled={!cursor || loading} onClick={() => route({ cursor: null })}>最新记录</button><button disabled={loading || !page?.has_more} onClick={() => route({ cursor: page?.next_cursor || null })}>更早记录</button></footer></aside>
    <main className="qfb-detail">{selected ? <><header className="qfb-detail-head"><div><h2>{selected.engine_kind === "rust" ? "Rust 运行" : "旧引擎历史只读"}</h2><code>{selected.run_id}</code></div><div className="qfb-actions"><Link to={`/admin/backtest-runs/${selected.run_id}/results`} state={{from: location.pathname + location.search}}>查看结果</Link><Link to="/admin/backtest-compare" state={{from: location.pathname + location.search}}>结果比较</Link>{selected.engine_kind === "rust" ? terminal(selected.status) ? <button disabled={busy || pending} onClick={() => setDrawer({ source: selected })}>重新运行</button> : <button disabled={busy || selected.cancel_requested} onClick={() => void perform("cancel")}>{selected.cancel_requested ? "取消处理中" : "取消运行"}</button> : <Link to={`/admin/backtest-runs/${selected.run_id}/results`}>查看原始历史结果</Link>}{selected.batch_id && <button onClick={() => route({ batch: selected.batch_id!, batchCursor: null })}>查看参数批次</button>}</div></header>
      <div className="qfb-preview"><section className="qfb-card"><header><h3>运行进度</h3><span role="status">{selected.cancel_requested && !terminal(selected.status) ? "取消处理中" : STATUS[selected.status] || "未知状态"}</span></header><div className="qf-run-padding">
        {selected.progress_total && selected.progress_completed !== undefined ? <><progress aria-label="运行进度" max={selected.progress_total} value={selected.progress_completed}/><p>{selected.progress_completed} / {selected.progress_total}</p></> : <p>服务尚未提供可量化进度，以运行状态为准。</p>}
        {selected.partial && <p>本次仅有部分结果，不能据此得出完整收益结论。</p>}
        {selected.engine_kind === "rust" && <p>订单受理不等于成交；拒单属于订单层，可与后续运行并存。运行失败、数据变化终止及能力缺失分别显示原因。</p>}
        {selected.error && <p role="alert">{api.issueText(selected.error)} <code>{api.issueCode(selected.error)}</code>{api.issueLocation(selected.error) && <small>{api.issueLocation(selected.error)}</small>}</p>}
        {selected.engine_kind === "legacy_python" && <p>保留原策略、账户与结果，不用 Rust 重算旧指标。迁入需另存新的 S3 修订。</p>}
      </div></section>
      {config && <section className="qfb-card"><header><h3>本次运行配置</h3></header><dl><dt>初始资金</dt><dd>{config.initial_cash} CNY</dd><dt>日期 / 频率</dt><dd>{config.start} — {config.end} · {config.frequency}</dd><dt>标的集合</dt><dd>{config.universe.join("、")}</dd><dt>策略修订</dt><dd>{config.strategy_revision_id}</dd><dt>账户配置</dt><dd>{config.account_id} · v{selected.account_version ?? "—"}</dd><dt>撮合模型</dt><dd>{modelName[config.execution_model]}</dd><dt>模型假设</dt><dd>参与率 {config.participation_rate} · 滑点 {config.slippage_bps} 基点</dd><dt>统计假设</dt><dd>年化会话 {config.annualization_sessions} · 无风险利率 {config.risk_free_rate} · seed {config.seed}</dd><dt>参考日历</dt><dd>{config.reference_calendar || "尚未确认"}</dd><dt>费用</dt><dd>使用服务固定的账户费用配置及日期规则。{config.cost_overrides && `佣金率覆盖 ${config.cost_overrides.commission_rate ?? "沿用账户"}；最低佣金覆盖 ${config.cost_overrides.minimum_commission ?? "沿用账户"}`}</dd></dl><p className="qfb-note">当前修订数据不是历史数据快照。受理后服务仍检查数据变化与读取条件；零滑点不等于零费用。</p></section>}
      {summary && <section className="qfb-card"><header><h3>结果存储状态</h3></header><p className="qf-run-padding">{summary.partial || summary.summary === null ? "服务返回部分结果或无完整摘要，不能据此计算完整收益。" : "服务已保存本次结果摘要。运行 ID 可用于 SDK / CLI 分页读取结果。"}</p></section>}
      </div></> : <div className="qfb-empty">{detailLoading ? "正在加载所选运行…" : "选择运行查看进度与配置"}</div>}
    </main></div>
    {batchId && !denied && <section className="qfb-card qf-run-batch"><header><div><h2>参数批次</h2><code>{batchId}</code></div><div className="qfb-actions"><button disabled={busy} onClick={() => void perform("cancelBatch")}>取消未终态项</button><button disabled={busy || pending || !batch?.items.some(r => r.status === "failed")} onClick={() => void perform("retryBatch")}>仅重跑失败项</button><Link to={`/admin/backtest-compare?batch=${encodeURIComponent(batchId)}`} state={{from: location.pathname + location.search}}>比较批次结果</Link><button onClick={() => route({ batch: null, batchCursor: null })}>关闭批次</button></div></header><div className="qf-run-batch-rows">{batch ? batch.items.map(row => <button key={row.run_id} onClick={() => route({ run: row.run_id })}><code>{row.run_id}</code><span>{STATUS[row.status] || "未知状态"}{row.retry_of ? " · 失败项重跑" : ""}</span>{row.error && <small>{api.issueText(row.error)}</small>}</button>) : <p>正在加载批次…</p>}</div><div className="qfb-actions qf-run-padding"><button disabled={!batchCursor} onClick={() => route({ batchCursor: null })}>批次首页</button><button disabled={!batch?.has_more} onClick={() => route({ batchCursor: batch?.next_cursor || null })}>批次下一页</button></div><p className="qfb-note">已完成结果保留。每项失败独立展示；重跑生成关联原项的新运行。</p></section>}
    {drawer && <CreateRunDrawer initialStrategyId={strategyId} source={drawer.source} onClose={() => setDrawer(null)} onCreated={accepted}/>}
  </div>;
}
