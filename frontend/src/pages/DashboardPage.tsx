import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { loadOverviewSnapshot, OverviewApiError, type OverviewSnapshot } from "../api/overview";
import { useAuth } from "../auth/AuthContext";
import { OverviewContent } from "../components/OverviewContent";

const emptySnapshot: OverviewSnapshot = { operations: null, etfs: null, calendar: null };

export function DashboardPage() {
  const { logout } = useAuth();
  const navigate = useNavigate();
  const [snapshot, setSnapshot] = useState<OverviewSnapshot>(emptySnapshot);
  const [loading, setLoading] = useState(true);
  const [errors, setErrors] = useState<string[]>([]);
  const [refreshed, setRefreshed] = useState(false);
  const snapshotRef = useRef(snapshot);
  const activeRequest = useRef<AbortController | null>(null);
  const load = useCallback(async () => {
    // Abort superseded work and ignore its completion, including its finally
    // block. This handles route unmounts, StrictMode and rapid refreshes alike.
    activeRequest.current?.abort();
    const controller = new AbortController();
    activeRequest.current = controller;
    setLoading(true);
    try {
      const result = await loadOverviewSnapshot(snapshotRef.current, controller.signal);
      if (activeRequest.current !== controller) return;
      snapshotRef.current = result.snapshot;
      setSnapshot(result.snapshot); setErrors(result.errors); setRefreshed(true);
    } catch (error) {
      if (activeRequest.current !== controller) return;
      if (error instanceof OverviewApiError && error.status === 401) {
        logout(); navigate("/login", { replace: true }); return;
      }
      if (!controller.signal.aborted) {
        setErrors(["运营指标、数据源与运行记录", "ETF 资产", "交易日历资产"]);
      }
    } finally {
      if (activeRequest.current === controller) setLoading(false);
    }
  }, [logout, navigate]);
  useEffect(() => {
    void load();
    return () => { activeRequest.current?.abort(); activeRequest.current = null; };
  }, [load]);
  return <OverviewContent snapshot={snapshot} loading={loading} errors={errors} refreshed={refreshed} onRefresh={() => void load()} />;
}
