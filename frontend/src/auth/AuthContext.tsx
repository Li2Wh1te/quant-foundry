import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState
} from "react";

import { verifyApiToken } from "../api/auth";
import {
  readApiToken,
  removeApiToken,
  writeApiToken
} from "./tokenStorage";

type AuthStatus = "checking" | "authenticated" | "anonymous";

interface AuthContextValue {
  status: AuthStatus;
  login: (token: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const storedToken = useMemo(() => readApiToken(), []);
  const [status, setStatus] = useState<AuthStatus>(
    storedToken ? "checking" : "anonymous"
  );

  useEffect(() => {
    if (!storedToken) {
      return;
    }

    const controller = new AbortController();
    verifyApiToken(storedToken, controller.signal)
      .then(() => setStatus("authenticated"))
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") {
          return;
        }
        removeApiToken();
        setStatus("anonymous");
      });

    return () => controller.abort();
  }, [storedToken]);

  const login = useCallback(async (token: string) => {
    const normalizedToken = token.trim();
    await verifyApiToken(normalizedToken);
    writeApiToken(normalizedToken);
    setStatus("authenticated");
  }, []);

  const logout = useCallback(() => {
    removeApiToken();
    setStatus("anonymous");
  }, []);

  const value = useMemo(
    () => ({ status, login, logout }),
    [status, login, logout]
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
