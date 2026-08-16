import { Navigate, Route, Routes } from "react-router-dom";

import { LoadingScreen } from "./components/LoadingScreen";
import { VersionMismatchScreen } from "./components/VersionMismatchScreen";
import { useAuth } from "./auth/AuthContext";
import { AdminPage } from "./pages/AdminPage";
import { DataCollectionPage, EtfBasicsPage } from "./pages/DataCollectionPage";
import { EtfDetailPage } from "./pages/EtfDetailPage";
import { LoginPage } from "./pages/LoginPage";
import { LogPage } from "./pages/LogPage";
import { StrategyDataApiPage } from "./pages/StrategyDataApiPage";
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
            <AdminPage />
          </RequireAuth>
        }
      />
      <Route
        path="/admin/logs"
        element={
          <RequireAuth>
            <AdminPage><LogPage /></AdminPage>
          </RequireAuth>
        }
      />
      <Route
        path="/admin/tasks"
        element={
          <RequireAuth>
            <AdminPage><TaskSchedulerPage /></AdminPage>
          </RequireAuth>
        }
      />
      <Route
        path="/admin/data/trading-calendar"
        element={<RequireAuth><AdminPage><DataCollectionPage page="trading-calendar" /></AdminPage></RequireAuth>}
      />
      <Route
        path="/admin/data/daily-quotes"
        element={<RequireAuth><AdminPage><DataCollectionPage page="daily-quotes" /></AdminPage></RequireAuth>}
      />
      <Route
        path="/admin/data/etf-basics"
        element={<RequireAuth><AdminPage><EtfBasicsPage /></AdminPage></RequireAuth>}
      />
      <Route
        path="/admin/data/etf-basics/:tsCode"
        element={<RequireAuth><AdminPage><EtfDetailPage /></AdminPage></RequireAuth>}
      />
      <Route
        path="/admin/strategy-data"
        element={<RequireAuth><AdminPage><StrategyDataApiPage /></AdminPage></RequireAuth>}
      />
      <Route path="*" element={<Navigate to="/admin" replace />} />
    </Routes>
  );
}
