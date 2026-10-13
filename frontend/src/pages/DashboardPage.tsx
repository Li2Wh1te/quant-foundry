import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { getOperationsOverview, OverviewApiError, type OperationsOverview } from "../api/overview";
import { useAuth } from "../auth/AuthContext";
import { OverviewContent } from "../components/OverviewContent";
import { marketClient, onMarketInvalidation } from "../features/market-data/data";
import { useMarketResource } from "../features/market-data/catalog/useMarketResource";
import { ETF_CATALOG } from "../features/market-data/catalog/paging";

const OPERATIONS = "运营指标、数据源与运行记录";

export function DashboardPage() {
  const { logout } = useAuth();
  const navigate = useNavigate();
  const [operations, setOperations] = useState<OperationsOverview | null>(null);
  const [loading, setLoading] = useState(true), [errors, setErrors] = useState<string[]>([]);
  const [refreshed, setRefreshed] = useState(false), [revision, setRevision] = useState(0);
  const activeRequest = useRef<AbortController | null>(null);
  const onUnauthorized = useCallback(() => { logout(); navigate("/login", { replace: true }); }, [logout, navigate]);
  const readMarket = useCallback((signal: AbortSignal) => marketClient.overview(ETF_CATALOG, { signal }), []);
  const market = useMarketResource(`overview:${ETF_CATALOG}`, readMarket, revision, onUnauthorized);
  const load = useCallback(async () => {
    activeRequest.current?.abort();
    const controller = new AbortController(); activeRequest.current = controller; setLoading(true);
    try {
      const result = await getOperationsOverview(controller.signal);
      if (activeRequest.current !== controller || controller.signal.aborted) return;
      setOperations(result); setErrors([]); setRefreshed(true);
    } catch (error) {
      if (activeRequest.current !== controller) return;
      if (error instanceof OverviewApiError && error.status === 401) { onUnauthorized(); return; }
      if (!controller.signal.aborted) {
        if (!(error instanceof OverviewApiError && [0, 504].includes(error.status))) {
          setOperations(null);
        }
        setErrors([OPERATIONS]);
      }
    } finally {
      if (activeRequest.current === controller) setLoading(false);
    }
  }, [onUnauthorized]);
  useEffect(() => {
    void load();
    return () => { activeRequest.current?.abort(); activeRequest.current = null; };
  }, [load]);
  useEffect(() => onMarketInvalidation(reason => {
    activeRequest.current?.abort(); activeRequest.current = null;
    setOperations(null); setLoading(false);
    setErrors(reason === "session" ? [] : [OPERATIONS]);
    if (reason === "authentication") onUnauthorized();
  }), [onUnauthorized]);
  return <OverviewContent snapshot={{ operations, market }} loading={loading || market.pending}
    errors={errors} refreshed={refreshed} onRefresh={() => { setRevision(value => value + 1); void load(); }} />;
}
