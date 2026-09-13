import { useEffect, useState } from "react";
import { fetchBacktestResult, type BacktestRun, type BacktestResultPage } from "../../api/backtestRuns";
import { BacktestReport, EvidenceTable } from "../../components/BacktestReport";
const KINDS = { steps: "运行步骤", decisions: "策略决策", orders: "订单", fills: "成交", positions: "持仓", equity: "权益", metrics: "指标" };
/** Retain the existing detailed evidence while the result page awaits its own redesign. */
export function RunResults({ run }: { run: BacktestRun }) {
  const [kind, setKind] = useState<keyof typeof KINDS>("steps");
  const [page, setPage] = useState<BacktestResultPage | null>(null), [cursor, setCursor] = useState<string>(), [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController(); setPage(null); setError("");
    fetchBacktestResult(run.run_id, kind, cursor, controller.signal).then(value => {
      if (!controller.signal.aborted) setPage(value);
    }).catch(caught => { if (!controller.signal.aborted) setError(caught.message); });
    return () => controller.abort();
  }, [run.run_id, run.status, kind, cursor]);
  return <><BacktestReport run={run} />
    <EvidenceTable title="失败定位与运行诊断" value={{ failure_evidence: run.failure_evidence, failure_phase: run.failure_phase, failure_type: run.failure_type, source_line: run.source_line, technical_detail: run.technical_detail, heartbeat: run.last_heartbeat_at, worker: run.worker_id, recovery: run.recovery_process_state, stdout: run.stdout_evidence, resources: run.resource_limit_evidence }} />
    <section><h3>运行明细</h3><div className="qfb-actions">{Object.entries(KINDS).map(([key, label]) => <button key={key} aria-pressed={kind === key} onClick={() => { setKind(key as keyof typeof KINDS); setCursor(undefined); }}>{label}</button>)}</div>
      {error && <p role="alert">{error}</p>}
      {!page && !error && <p>正在加载明细…</p>}
      {page && <><p>{page.items.length ? `本页 ${page.items.length} 条记录` : "暂无记录"}</p><details open><summary>{KINDS[kind]}原始记录</summary><pre>{JSON.stringify(page.items, null, 2)}</pre></details>
        {cursor && <button onClick={() => setCursor(undefined)}>返回明细首页</button>}
        {page.next_cursor && <button onClick={() => setCursor(page.next_cursor!)}>下一页明细</button>}
      </>}
    </section>
  </>;
}
