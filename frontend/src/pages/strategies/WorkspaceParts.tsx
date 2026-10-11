import { isDialogBackdropClick } from "../../components/controls/dialogBackdrop";
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../../auth/AuthContext";
import { STATUS } from "../backtests/run-ui/config";
import { X } from "lucide-react";
import { listRuns, onRunAccessDenied, type RunSummary } from "../../api/runService";

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
  creation = false,
  centered = false,
  onClose,
  children,
}: {
  title: string;
  creation?: boolean;
  centered?: boolean;
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const prior = document.activeElement as HTMLElement;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    ref.current?.showModal();
    return () => {
      document.body.style.overflow = previousOverflow;
      if (prior?.isConnected) prior.focus();
    };
  }, []);
  return (
    <dialog
      className={`qfs-drawer${creation ? " qf-create-drawer" : ""}${centered ? " qfs-confirm-dialog" : ""}`}
      ref={ref}
      aria-label={title}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onClick={(e) => {
        if (isDialogBackdropClick(e)) onClose();
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
  const { logout } = useAuth(), navigate = useNavigate();
  const [rows, setRows] = useState<RunSummary[]>([]),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(true);
  useEffect(() => {
    const c = new AbortController();
    setLoading(true);
    setRows([]);
    setError("");
    const unsubscribe = onRunAccessDenied(status => {
      c.abort(); setRows([]); setLoading(false);
      setError(status === 401 ? "登录状态已失效。" : "没有回测查看权限。");
      if (status === 401) { logout(); navigate("/login", { replace: true }); }
    });
    listRuns(undefined, c.signal, strategyId).then(page => {
      // D13 returns the newest page first. One bounded request is enough.
      if (!c.signal.aborted) setRows(page.items.slice(0, 10));
    }).catch(e => {
      if (!c.signal.aborted) setError(e instanceof Error ? e.message : "回测历史加载失败。");
    }).finally(() => { if (!c.signal.aborted) setLoading(false); });
    return () => { unsubscribe(); c.abort(); };
  }, [strategyId, logout, navigate]);
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
            <Link to={`/admin/strategies/${strategyId}/backtests?run=${encodeURIComponent(r.run_id)}`}>
              {r.run_id}
            </Link>
            <small>
              {r.engine_kind === "rust" ? "Rust 回测" : "历史只读"} · {STATUS[r.status] || "未知状态"}
            </small>
            <small>版本引用：{r.normalized_config?.strategy_revision_id || r.strategy_revision_id || "—"}</small>
            <small>账户：{r.normalized_config?.account_id || r.account_profile_id || "未指定"} · v{r.account_version ?? r.account_profile_version ?? "—"}</small>
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

/** Keep unsaved-change confirmation mounted until an explicit user decision.
 * Native modal focus containment also prevents repeated background clicks from
 * replacing the blocked destination while the user reads the message. */
export function NavigationConfirm({ onCancel, onConfirm }: {
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const prior = document.activeElement as HTMLElement | null;
    dialogRef.current?.showModal();
    return () => {
      dialogRef.current?.close();
      if (prior?.isConnected) prior.focus();
    };
  }, []);
  return <dialog
    ref={dialogRef}
    className="qfs-drawer qfs-navigation-confirm"
    aria-labelledby="qfs-navigation-confirm-title"
    aria-describedby="qfs-navigation-confirm-description"
    onCancel={(event) => { event.preventDefault(); onCancel(); }}
  >
    <h2 id="qfs-navigation-confirm-title">放弃未保存的修改？</h2>
    <p id="qfs-navigation-confirm-description">当前草稿尚未保存。继续编辑可保留修改，放弃后将前往你选择的页面。</p>
    <footer>
      <button type="button" autoFocus onClick={onCancel}>继续编辑</button>
      <button type="button" className="qfs-primary" onClick={onConfirm}>放弃修改并切换</button>
    </footer>
  </dialog>;
}
