import { useCallback, useEffect, useState } from "react";
import { cancelBacktestRun, createBacktestRun, getBacktestRun, listBacktestRuns, type BacktestRun } from "../api/backtestRuns";

const TERMINAL = new Set(["succeeded", "failed", "cancelled", "timed_out", "terminal", "indeterminate"]);
const statusLabel: Record<string,string> = { queued:"排队中", starting:"启动中", running:"运行中", cancel_requested:"取消处理中", succeeded:"已成功", failed:"失败", cancelled:"已取消", timed_out:"已超时", indeterminate:"结果待判定", terminal:"已结束" };

export function BacktestRunsPage() {
  const [runs, setRuns] = useState<BacktestRun[]>([]);
  const [selected, setSelected] = useState<BacktestRun | null>(null);
  const [strategy, setStrategy] = useState("");
  const [message, setMessage] = useState("");

  const refresh = useCallback(async () => {
    try { const d = await listBacktestRuns(); setRuns((d.items || []).filter((r:BacktestRun) => r.run_kind !== "internal_link_acceptance")); }
    catch (e) { setMessage(e instanceof Error ? e.message : "回测列表加载失败。"); }
  }, []);
  const refreshDetail = useCallback(async (id: string) => { try { setSelected(await getBacktestRun(id)); } catch (e) { setMessage(e instanceof Error ? e.message : "回测详情加载失败。"); } }, []);

  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => {
    let timer: number | undefined;
    const tick = () => { if (document.visibilityState === "visible" && runs.some(r => !TERMINAL.has(r.status))) { refresh(); if (selected) refreshDetail(selected.id || selected.run_id || ""); } };
    const start = () => { if (document.visibilityState === "visible") { tick(); timer = window.setInterval(tick, 5000); } };
    const stop = () => { if (timer) window.clearInterval(timer); timer = undefined; };
    const onVisibility = () => { stop(); if (document.visibilityState === "visible") { tick(); start(); } };
    start(); document.addEventListener("visibilitychange", onVisibility); return () => { stop(); document.removeEventListener("visibilitychange", onVisibility); };
  }, [runs, selected, refresh, refreshDetail]);

  const create = () => { if (!strategy.trim()) { setMessage("请填写策略版本"); return; } setMessage("正在预检…"); createBacktestRun({ strategy_revision_id: strategy.trim(), idempotency_key: crypto.randomUUID(), spec: { start_date: new Date().toISOString().slice(0,10), end_date: new Date().toISOString().slice(0,10), initial_cash: "0", initial_positions: [] } }).then(() => { setMessage("已创建"); refresh(); }).catch(e => setMessage(e.message || "创建失败。")); };
  return <section><h1>回测运行</h1><label>策略版本 <input value={strategy} onChange={e=>setStrategy(e.target.value)} placeholder="strategy-revision" /></label><button onClick={create}>预检并创建回测</button>{message&&<p role="status">{message}</p>}
    {runs.map(r => <article key={r.id || r.run_id}><button onClick={() => refreshDetail(r.id || r.run_id || "")}><div>{r.id || r.run_id}</div><div>{statusLabel[r.status] || "运行状态"} {Math.round((r.progress||0)*100)}%</div><div>{r.current_date || ""}</div></button>{["queued","starting","running"].includes(r.status)&&<button onClick={()=>cancelBacktestRun(r.id || r.run_id || "").then(refresh).catch(e=>setMessage(e.message))}>取消</button>}</article>)}
    {selected && <aside aria-label="回测详情"><h2>回测详情</h2><p>状态：{statusLabel[selected.status] || selected.status}；当前交易日：{selected.current_date || "—"}；步骤：{selected.current_step ?? "—"}</p><p>完成比例：{Math.round((selected.progress||0)*100)}%；最后心跳：{selected.last_heartbeat_at || "—"}</p>{selected.error_message && <p role="alert">错误：{selected.error_message}</p>}<details><summary>技术详情</summary><pre>{JSON.stringify({ run_id:selected.id||selected.run_id, terminal_reason:selected.terminal_reason, completion_marker:selected.completion_marker }, null, 2)}</pre></details></aside>}
  </section>;
}
