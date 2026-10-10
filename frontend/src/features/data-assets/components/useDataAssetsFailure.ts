import { useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { DataStoreApiError } from "../../../api/dataStore";
import { useAuth } from "../../../auth/AuthContext";

/** Preserve the existing session flow while clearing content denied by the API. */
export function useDataAssetsFailure(clear: () => void) {
  const { logout } = useAuth();
  const navigate = useNavigate();
  return useCallback((error: unknown): string => {
    if (error instanceof DataStoreApiError && (error.status === 401 || error.status === 403)) {
      clear();
      if (error.status === 401) {
        logout();
        navigate("/login", { replace: true });
      } else {
        return "没有当前数据的读取权限，请联系管理员后刷新页面。";
      }
    }
    return error instanceof DataStoreApiError ? error.message : "读取失败，请刷新页面重试。";
  }, [clear, logout, navigate]);
}
