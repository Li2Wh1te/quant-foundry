import { Navigate, Route, Routes } from "react-router-dom";

import { LoadingScreen } from "./components/LoadingScreen";
import { useAuth } from "./auth/AuthContext";
import { AdminPage } from "./pages/AdminPage";
import { LoginPage } from "./pages/LoginPage";

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
      <Route path="*" element={<Navigate to="/admin" replace />} />
    </Routes>
  );
}
