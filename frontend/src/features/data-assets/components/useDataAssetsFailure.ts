import { useCallback, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { DataStoreApiError, errorPresentation, onDataAssetsInvalidation } from "../data";
import { useAuth } from "../../../auth/AuthContext";

/** Preserve the existing session flow while clearing content denied by the API. */
export function useDataAssetsFailure(clear: () => void, report: (message: string) => void) {
  const { logout } = useAuth();
  const navigate = useNavigate();
  const denied = useCallback((authentication: boolean) => {
    const message = authentication ? "登录状态已失效，请重新登录。"
      : "没有当前数据的读取权限，请联系管理员后刷新页面。";
    clear(); report(message);
    if (authentication) { logout(); navigate("/login", { replace: true }); }
    return message;
  }, [clear, report, logout, navigate]);
  useEffect(() => onDataAssetsInvalidation(reason => {
    // An HTTP denial revokes every page-owned cache immediately, even when it
    // originated in another mounted panel or its optional body never completes.
    if (reason === "session") clear();
    else denied(reason === "authentication");
  }), [clear, denied]);
  return useCallback((error: unknown): string => {
    if (error instanceof DataStoreApiError && (error.status === 401 || error.status === 403)) {
      return denied(error.status === 401);
    }
    return errorPresentation(error);
  }, [denied]);
}
