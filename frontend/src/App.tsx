import "./components/controls/CreationDrawers.css";
import { DataAssetsPage } from "./pages/DataAssetsPage";
import { Navigate, Route, Routes } from "react-router-dom";

import { LoadingScreen } from "./components/LoadingScreen";
import { OverviewShell } from "./components/OverviewShell";
import { VersionMismatchScreen } from "./components/VersionMismatchScreen";
import { useAuth } from "./auth/AuthContext";
import { AccountProfilesPage } from "./pages/AccountProfilesPage";
import { BacktestRunsPage } from "./pages/BacktestRunsPage";
import { BacktestComparePage } from "./pages/BacktestComparePage";
import { BacktestResultPage } from "./pages/BacktestResultPage";
import { DashboardPage } from "./pages/DashboardPage";
import { DataSourcesPage } from "./pages/DataSourcesPage";
import { MarketPage } from "./pages/MarketPage";
import { EtfDetailPage } from "./pages/EtfDetailPage";
import { LoginPage } from "./pages/LoginPage";
import { StrategyDataApiPage } from "./pages/StrategyDataApiPage";
import { StrategiesPage } from "./pages/StrategiesPage";
import { TaskSchedulerPage } from "./pages/TaskSchedulerPage";

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { status } = useAuth();

  if (status === "checking") {
    return <LoadingScreen />;
  }
  if (status === "anonymous") {
    return <Navigate to="/login" replace />;
  }
  if (status === "version_mismatch") {
    return <VersionMismatchScreen />;
  }
  return children;
}

export function App() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/admin" replace />} />
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/admin"
        element={
          <RequireAuth>
            <OverviewShell><DashboardPage /></OverviewShell>
          </RequireAuth>
        }
      />
      {["/admin/data-assets", "/admin/data-assets/:datasetId", "/admin/data-assets/:datasetId/processing"].map(path => <Route key={path} path={path} element={<RequireAuth><OverviewShell title="数据资产" section="MARKET DATA" className="qf-assets-root"><DataAssetsPage /></OverviewShell></RequireAuth>} />)
      }
      <Route path="/admin/data-sources" element={<RequireAuth><OverviewShell title="数据源" section="DATA OPS"><DataSourcesPage /></OverviewShell></RequireAuth>} />
      <Route
        path="/admin/tasks"
        element={
          <RequireAuth>
            <OverviewShell title="采集任务" section="DATA OPS"><TaskSchedulerPage /></OverviewShell>
          </RequireAuth>
        }
      />
      <Route
        path="/admin/data/trading-calendar"
        element={<RequireAuth><Navigate to="/admin/data/etf-basics?calendar=open" replace /></RequireAuth>}
      />
      <Route
        path="/admin/data/etf-basics"
        element={<RequireAuth><OverviewShell title="A 股市场" section="MARKET DATA" className="qfm-root"><MarketPage /></OverviewShell></RequireAuth>}
      />
      <Route
        path="/admin/data/etf-basics/:tsCode"
        element={<RequireAuth><EtfDetailPage /></RequireAuth>}
      />
      <Route
        path="/admin/strategy-data"
        element={<RequireAuth><OverviewShell title="策略数据接口" section="RESEARCH / 02" className="qfa-root"><StrategyDataApiPage /></OverviewShell></RequireAuth>}
      />
      <Route
        path="/admin/strategies"
        element={<RequireAuth><StrategiesPage /></RequireAuth>}
      />
      <Route
        path="/admin/backtest-accounts"
        element={<RequireAuth><OverviewShell title="回测账户" section="RESEARCH / 03" className="qfac-root"><AccountProfilesPage /></OverviewShell></RequireAuth>}
      />
      <Route
        path="/admin/backtest-preflight"
        element={<RequireAuth><Navigate to="/admin/backtest-runs" replace /></RequireAuth>}
      />
      <Route path="/admin/backtest-compare" element={<RequireAuth><OverviewShell title="回测对比" section="RESEARCH / 04" className="qcmp-root"><BacktestComparePage /></OverviewShell></RequireAuth>} />
      <Route path="/admin/backtest-runs/:runId/results" element={<RequireAuth><OverviewShell title="回测结果" section="RESEARCH / 04" className="qfb-root qfr-root"><BacktestResultPage /></OverviewShell></RequireAuth>} />
      <Route path="/admin/backtest-runs" element={<RequireAuth><OverviewShell title="回测工作台" section="RESEARCH / 04" className="qfb-root"><BacktestRunsPage /></OverviewShell></RequireAuth>} />
      <Route
        path="/admin/strategies/:strategyId"
        element={<RequireAuth><StrategiesPage /></RequireAuth>}
      />
      <Route path="/admin/strategies/:strategyId/backtests" element={<RequireAuth><OverviewShell title="回测工作台" section="RESEARCH / 04" className="qfb-root"><BacktestRunsPage /></OverviewShell></RequireAuth>} />
      <Route path="*" element={<Navigate to="/admin" replace />} />
    </Routes>
  );
}
