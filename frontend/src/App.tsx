import { Navigate, Route, Routes } from "react-router-dom";

import { LoadingScreen } from "./components/LoadingScreen";
import { useAuth } from "./auth/AuthContext";
import { AdminPage } from "./pages/AdminPage";
import { DataCollectionPage } from "./pages/DataCollectionPage";
import { LoginPage } from "./pages/LoginPage";
import { LogPage } from "./pages/LogPage";
import { TaskSchedulerPage } from "./pages/TaskSchedulerPage";

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { status } = useAuth();

  if (status === "checking") {
    return <LoadingScreen />;
  }
  if (status === "anonymous") {
    return <Navigate to="/login" replace />;
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
      <Route path="*" element={<Navigate to="/admin" replace />} />
    </Routes>
  );
}
