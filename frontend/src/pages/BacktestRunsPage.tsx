import { useCallback, useEffect, useRef, useState } from "react";

import {
  cancelBacktestRun,
  createBacktestRun,
  FOREGROUND_POLL_INTERVAL_MS,
  FOREGROUND_POLLING_PROTOCOL,
  getBacktestRun,
  isTerminalBacktestStatus,
  listBacktestRuns,
  type BacktestRun
} from "../api/backtestRuns";

const statusLabel: Record<string, string> = {
  queued: "排队中",
  starting: "启动中",
  running: "运行中",
  cancel_requested: "取消处理中",
  succeeded: "已成功",
  failed: "失败",
  cancelled: "已取消",
  timed_out: "已超时",
  indeterminate: "结果待判定"
};

function formatStatus(status: string): string {
  // Never expose a new internal status key as the primary operator-facing
  // text. Technical evidence remains available in the expanded details.
  return statusLabel[status] || "运行状态（待识别）";
}

function percent(progress: number | undefined): number {
  return Math.round(Math.max(0, Math.min(1, progress ?? 0)) * 100);
}

function evidence(run: BacktestRun): Record<string, unknown> {
  return {
    run_id: run.run_id,
    status: run.status,
    child_exit_code: run.child_exit_code,
    child_exit_code_protocol: run.child_exit_code_protocol,
    runner_exit_category: run.runner_exit_category,
    completion_marker: run.completion_marker,
    completion_marker_protocol: run.completion_marker_protocol,
    completion_marker_validation: run.completion_marker_validation,
    result_integrity_status: run.result_integrity_status,
    result_integrity_evidence: run.result_integrity_evidence,
    result_counts: run.result_counts,
    terminal_decision_reason: run.terminal_decision_reason,
    failure_phase: run.failure_phase,
    failure_type: run.failure_type,
    error_message: run.error_message,
    recovery_action: run.recovery_action,
    recovery_process_state: run.recovery_process_state,
    runner_exit_report: run.runner_exit_report,
    resource_limit_evidence: run.resource_limit_evidence
  };
}

export function BacktestRunsPage() {
  const [runs, setRuns] = useState<BacktestRun[]>([]);
  const [selected, setSelected] = useState<BacktestRun | null>(null);
  const [strategy, setStrategy] = useState("");
  const [message, setMessage] = useState("");

  // Refs keep the polling loop stable while allowing it to observe the latest
  // list/detail state without restarting the interval after every response.
  const selectedRef = useRef<BacktestRun | null>(null);
  const activeRunsRef = useRef(false);
  const pollInFlightRef = useRef(false);
  const mountedRef = useRef(true);
  const pollAbortRef = useRef<AbortController | null>(null);
  const pollGenerationRef = useRef(0);
  const startPollingRef = useRef<(() => void) | null>(null);
  const listInFlightRef = useRef(false);
  const detailInFlightRef = useRef(false);
  const listRequestGenerationRef = useRef(0);
  const detailRequestGenerationRef = useRef(0);

  useEffect(() => {
    selectedRef.current = selected;
  }, [selected]);

  useEffect(() => {
    activeRunsRef.current = runs.some((run) => !isTerminalBacktestStatus(run.status));
  }, [runs]);

  const refresh = useCallback(async (signal?: AbortSignal): Promise<BacktestRun[]> => {
    if (listInFlightRef.current) return [];
    listInFlightRef.current = true;
    const requestGeneration = ++listRequestGenerationRef.current;
    try {
      const data = await listBacktestRuns(signal);
      const items = (data.items || []).filter((run) => run.run_kind !== "internal_link_acceptance");
      activeRunsRef.current = items.some((run) => !isTerminalBacktestStatus(run.status));
      if (mountedRef.current) setRuns(items);
      return items;
    } catch (error) {
      if (signal?.aborted) return [];
      if (mountedRef.current) setMessage(error instanceof Error ? error.message : "回测列表加载失败。");
      return [];
    } finally {
      if (listRequestGenerationRef.current === requestGeneration) {
        listInFlightRef.current = false;
      }
    }
  }, []);

  const refreshDetail = useCallback(async (id: string, signal?: AbortSignal): Promise<void> => {
    if (!id || detailInFlightRef.current) return;
    detailInFlightRef.current = true;
    const requestGeneration = ++detailRequestGenerationRef.current;
    try {
      const detail = await getBacktestRun(id, signal);
      // A user may select another run while this request is in flight. Do
      // not let the stale response replace that newer selection.
      const selectedId = selectedRef.current?.run_id;
      if (selectedId === undefined || selectedId === id) {
        selectedRef.current = detail;
        if (mountedRef.current) setSelected(detail);
      }
    } catch (error) {
      if (signal?.aborted) return;
      if (mountedRef.current) setMessage(error instanceof Error ? error.message : "回测详情加载失败。");
    } finally {
      if (detailRequestGenerationRef.current === requestGeneration) {
        detailInFlightRef.current = false;
      }
    }
  }, []);

  const poll = useCallback(async (force = false): Promise<void> => {
    // Visibility is checked immediately before scheduling network work. The
    // in-flight guard prevents list/detail requests from overlapping when a
    // slow response crosses a timer boundary.
    if (document.visibilityState !== "visible" || pollInFlightRef.current) return;
    if (!force && !activeRunsRef.current && !selectedRef.current) return;
    pollInFlightRef.current = true;
    const generation = ++pollGenerationRef.current;
    const controller = new AbortController();
    pollAbortRef.current = controller;
    try {
      const items = await refresh(controller.signal);
      if (controller.signal.aborted) return;
      const selectedRun = selectedRef.current;
      if (!selectedRun || isTerminalBacktestStatus(selectedRun.status)) return;
      // Refresh the detail only while it is live. Once a terminal response is
      // observed, the list response also stops the foreground loop.
      if (items.some((run) => run.run_id === selectedRun.run_id)) {
        await refreshDetail(selectedRun.run_id, controller.signal);
      }
    } finally {
      if (pollAbortRef.current === controller) pollAbortRef.current = null;
      if (pollGenerationRef.current === generation) pollInFlightRef.current = false;
    }
  }, [refresh, refreshDetail]);

  useEffect(() => {
    mountedRef.current = true;
    let timer: number | undefined;

    const hasLiveSelection = () => {
      const current = selectedRef.current;
      return current !== null && !isTerminalBacktestStatus(current.status);
    };

    const stop = () => {
      if (timer !== undefined) {
        window.clearInterval(timer);
        timer = undefined;
      }
      // Invalidate an in-flight poll before aborting it, so a foreground
      // transition can issue its required immediate request without waiting
      // for an aborted fetch's microtask to settle.
      pollGenerationRef.current += 1;
      pollInFlightRef.current = false;
      // Release request gates synchronously after aborting ownership. The
      // generation checks in each request's finally block prevent the old
      // aborted promise from clearing a newer request's gate.
      listRequestGenerationRef.current += 1;
      detailRequestGenerationRef.current += 1;
      listInFlightRef.current = false;
      detailInFlightRef.current = false;
      pollAbortRef.current?.abort();
    };

    const start = () => {
      if (document.visibilityState !== "visible" || timer !== undefined) return;
      // Initial entry into the foreground is an immediate request; subsequent
      // requests occur at the fixed five-second protocol interval.
      const tick = async (immediate = false) => {
        await poll(immediate);
        // Terminal status is authoritative from the API. Once no live run is
        // left, clear the timer instead of continuing no-op requests.
        if (!activeRunsRef.current && !hasLiveSelection()) stop();
      };
      void tick(true);
      timer = window.setInterval(() => void tick(), FOREGROUND_POLL_INTERVAL_MS);
    };

    startPollingRef.current = start;

    const onVisibilityChange = () => {
      stop();
      if (document.visibilityState === "visible") start();
    };

    if (document.visibilityState === "visible") start();
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      mountedRef.current = false;
      startPollingRef.current = null;
      stop();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [poll]);

  const create = () => {
    if (!strategy.trim()) {
      setMessage("请填写策略版本");
      return;
    }
    setMessage("正在预检…");
    void createBacktestRun({
      strategy_revision_id: strategy.trim(),
      idempotency_key: crypto.randomUUID(),
      spec: {
        start_date: new Date().toISOString().slice(0, 10),
        end_date: new Date().toISOString().slice(0, 10),
        initial_cash: "0",
        initial_positions: []
      }
    }).then(async () => {
      setMessage("已创建");
      const items = await refresh();
      if (items.some((run) => !isTerminalBacktestStatus(run.status))) {
        startPollingRef.current?.();
      }
    }).catch((error: unknown) => {
      setMessage(error instanceof Error ? error.message : "创建失败。");
    });
  };

  const openDetail = (run: BacktestRun) => {
    selectedRef.current = run;
    setSelected(run);
    void refreshDetail(run.run_id);
  };

  return (
    <section data-polling-protocol={FOREGROUND_POLLING_PROTOCOL}>
      <h1>回测运行</h1>
      <label>
        策略版本 <input value={strategy} onChange={(event) => setStrategy(event.target.value)} placeholder="strategy-revision" />
      </label>
      <button type="button" onClick={create}>预检并创建回测</button>
      {message && <p role="status">{message}</p>}

      {runs.map((run) => (
        <article key={run.run_id}>
          <button type="button" onClick={() => openDetail(run)}>
            <div>{run.run_id}</div>
            <div>{formatStatus(run.status)} {percent(run.progress_ratio)}%</div>
            <div>{run.current_trading_date || ""}</div>
          </button>
          {["queued", "starting", "running", "cancel_requested"].includes(run.status) && (
            <button
              type="button"
              onClick={() => void cancelBacktestRun(run.run_id).then(() => refresh()).catch((error: unknown) => setMessage(error instanceof Error ? error.message : "取消失败。"))}
            >取消</button>
          )}
        </article>
      ))}

      {selected && (
        <aside aria-label="回测详情">
          <h2>回测详情</h2>
          <p>状态：{formatStatus(selected.status)}；当前交易日：{selected.current_trading_date || "—"}；步骤：{selected.current_step ?? "—"}</p>
          <p>完成比例：{percent(selected.progress_ratio)}%；最后心跳：{selected.last_heartbeat_at || "—"}</p>
          {selected.error_message && <p role="alert">错误：运行异常，具体原因请查看技术详情。</p>}
          <details>
            <summary>技术详情</summary>
            <pre>{JSON.stringify(evidence(selected), null, 2)}</pre>
          </details>
        </aside>
      )}
    </section>
  );
}
