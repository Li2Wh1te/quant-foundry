import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { X } from "lucide-react";
import {
  fetchStrategyBacktestWorkspace,
  type BacktestRun,
} from "../../api/backtestRuns";

export function ResizeHandle({
  label,
  value,
  min,
  max,
  onChange,
  horizontal = false,
  reverse = false,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  onChange: (n: number) => void;
  horizontal?: boolean;
  reverse?: boolean;
}) {
  const drag = useRef<{ position: number; value: number } | null>(null);
  return (
    <div
      className={`qfs-resize ${horizontal ? "qfs-resize-y" : ""}`}
      role="separator"
      tabIndex={0}
      aria-label={label}
      aria-orientation={horizontal ? "horizontal" : "vertical"}
      aria-valuenow={value}
      aria-valuemin={min}
      aria-valuemax={max}
      onKeyDown={(e) => {
        const d =
          e.key === "ArrowRight" || e.key === "ArrowDown"
            ? 10
            : e.key === "ArrowLeft" || e.key === "ArrowUp"
              ? -10
              : 0;
        if (d) {
          e.preventDefault();
          onChange(
            Math.min(max, Math.max(min, value + d * (reverse ? -1 : 1))),
          );
        }
      }}
      onPointerDown={(e) => {
        drag.current = { position: horizontal ? e.clientY : e.clientX, value };
        e.currentTarget.setPointerCapture(e.pointerId);
      }}
      onPointerMove={(e) => {
        if (drag.current)
          onChange(
            Math.min(
              max,
              Math.max(
                min,
                drag.current.value +
                  ((horizontal ? e.clientY : e.clientX) -
                    drag.current.position) *
                    (reverse ? -1 : 1),
              ),
            ),
          );
      }}
      onPointerUp={() => {
        drag.current = null;
      }}
      onLostPointerCapture={() => {
        drag.current = null;
      }}
    />
  );
}
export function Drawer({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const prior = document.activeElement as HTMLElement;
    ref.current?.showModal();
    return () => {
      if (prior?.isConnected) prior.focus();
    };
  }, []);
  return (
    <dialog
      className="qfs-drawer"
      ref={ref}
      aria-label={title}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget) {
          const r = e.currentTarget.getBoundingClientRect();
          if (e.clientX < r.left || e.clientX > r.right) onClose();
        }
      }}
    >
      <header>
        <div>
          <small>STRATEGY WORKSPACE</small>
          <h2>{title}</h2>
        </div>
        <button type="button" onClick={onClose} aria-label={`关闭${title}`}>
          <X />
        </button>
      </header>
      {children}
    </dialog>
  );
}
export function RecentRuns({ strategyId }: { strategyId: string }) {
  const [rows, setRows] = useState<BacktestRun[]>([]),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(true);
  useEffect(() => {
    const c = new AbortController();
    setLoading(true);
    setRows([]);
    setError("");
    (async () => {
      try {
        let cursor: string | undefined;
        let recent: BacktestRun[] = [];
        // The existing cursor API is ascending and snapshot-bounded.
        // Walk it to retain the actual latest ten without inventing a total.
        do {
          const workspace = await fetchStrategyBacktestWorkspace(
            strategyId,
            c.signal,
            cursor,
          );
          if (c.signal.aborted) return;
          recent = [...recent, ...workspace.runs.items].slice(-10);
          const next = workspace.runs.has_more
            ? workspace.runs.next_cursor
            : undefined;
          if (workspace.runs.has_more && (!next || next === cursor))
            throw new Error("回测历史分页无效，请重试。");
          cursor = next || undefined;
        } while (cursor);
        setRows(recent.reverse());
      } catch (e) {
        if (!c.signal.aborted)
          setError(e instanceof Error ? e.message : "回测历史加载失败。");
      } finally {
        if (!c.signal.aborted) setLoading(false);
      }
    })();
    return () => c.abort();
  }, [strategyId]);
  return (
    <>
      <h3>最近回测</h3>
      <p className="qfs-muted">
        最多展示最近 10 次运行，账户引用属于对应回测。
      </p>
      {loading ? (
        <p role="status">加载中…</p>
      ) : error ? (
        <p role="alert">{error}</p>
      ) : !rows.length ? (
        <p>暂无回测记录</p>
      ) : (
        rows.map((r) => (
          <div className="qfs-record" key={r.run_id}>
            <Link to={`/admin/strategies/${strategyId}/backtests`}>
              {r.label || r.run_id}
            </Link>
            <small>
              状态：
              {(
                {
                  succeeded: "成功",
                  failed: "失败",
                  running: "运行中",
                  queued: "排队中",
                  cancelled: "已取消",
                } as Record<string, string>
              )[r.status] || r.status}
            </small>
            <small>版本引用：{r.strategy_revision_id || "—"}</small>
            <small>
              账户：{r.account_profile_id || "未指定"}
              {r.account_profile_version
                ? ` · v${r.account_profile_version}`
                : ""}
            </small>
          </div>
        ))
      )}
      <Link to={`/admin/strategies/${strategyId}/backtests`}>
        查看回测工作台 →
      </Link>
      <h3>策略数据接口</h3>
      <p className="qfs-muted">查询能力与使用说明</p>
      <Link to="/admin/strategy-data">打开接口文档 →</Link>
    </>
  );
}
