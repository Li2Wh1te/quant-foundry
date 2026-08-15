import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState
} from "react";

import { fetchSystemVersion, verifyApiToken } from "../api/auth";
import {
  readApiToken,
  removeApiToken,
  writeApiToken
} from "./tokenStorage";
import { FRONTEND_VERSION } from "../version";

type AuthStatus = "checking" | "authenticated" | "anonymous" | "version_mismatch";

interface AuthContextValue {
  status: AuthStatus;
  login: (token: string) => Promise<void>;
  logout: () => void;
  backendVersion: string | null;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const storedToken = useMemo(() => readApiToken(), []);
  const [status, setStatus] = useState<AuthStatus>(
    storedToken ? "checking" : "anonymous"
  );
  const [backendVersion, setBackendVersion] = useState<string | null>(null);

  const validateSession = useCallback(async (token: string, signal?: AbortSignal) => {
    await verifyApiToken(token, signal);
    const { version } = await fetchSystemVersion(token, signal);
    setBackendVersion(version);
    setStatus(version === FRONTEND_VERSION ? "authenticated" : "version_mismatch");
  }, []);

  useEffect(() => {
    if (!storedToken) {
      return;
    }

    const controller = new AbortController();
    validateSession(storedToken, controller.signal)
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") {
          return;
        }
        removeApiToken();
        setBackendVersion(null);
        setStatus("anonymous");
      });

    return () => controller.abort();
  }, [storedToken, validateSession]);

  const login = useCallback(async (token: string) => {
    const normalizedToken = token.trim();
    await validateSession(normalizedToken);
    writeApiToken(normalizedToken);
  }, [validateSession]);

  const logout = useCallback(() => {
    removeApiToken();
    setBackendVersion(null);
    setStatus("anonymous");
  }, []);

  const value = useMemo(
    () => ({ status, login, logout, backendVersion }),
    [status, login, logout, backendVersion]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used inside AuthProvider");
  }
  return context;
}
