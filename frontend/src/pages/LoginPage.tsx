import { Eye, EyeOff, LoaderCircle } from "lucide-react";
import { FormEvent, useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { Brand } from "../components/Brand";
import { LoadingScreen } from "../components/LoadingScreen";

export function LoginPage() {
  const { status, login } = useAuth();
  const navigate = useNavigate();
  const [token, setToken] = useState("");
  const [showToken, setShowToken] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (status === "checking") {
    return <LoadingScreen />;
  }
  if (status === "authenticated") {
    return <Navigate to="/admin" replace />;
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token.trim() || submitting) {
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      await login(token);
      navigate("/admin", { replace: true });
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "登录失败，请稍后重试。"
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="login-page">
      <header className="login-page__header">
        <Brand />
      </header>

      <section className="login-page__content">
        <form className="login-panel" onSubmit={handleSubmit} noValidate>
          <h1>进入管理控制台</h1>
          <p>使用部署时生成的 API Token 验证身份。</p>

          <label htmlFor="api-token">API Token</label>
          <div className="token-field">
            <input
              id="api-token"
              type={showToken ? "text" : "password"}
              value={token}
              onChange={(event) => setToken(event.target.value)}
              placeholder="输入 API Token"
              autoComplete="off"
              autoFocus
              spellCheck={false}
              aria-invalid={Boolean(error)}
              aria-describedby={error ? "login-error" : undefined}
            />
            <button
              type="button"
              onClick={() => setShowToken((visible) => !visible)}
              aria-label={showToken ? "隐藏 Token" : "显示 Token"}
              title={showToken ? "隐藏 Token" : "显示 Token"}
            >
              {showToken ? <EyeOff aria-hidden="true" /> : <Eye aria-hidden="true" />}
            </button>
          </div>

          <div className="login-panel__message" aria-live="polite">
            {error && (
              <span id="login-error" role="alert">
                {error}
              </span>
            )}
          </div>

          <button
            className="primary-button"
            type="submit"
            disabled={!token.trim() || submitting}
          >
            {submitting && <LoaderCircle className="spin" aria-hidden="true" />}
            {submitting ? "正在验证" : "登录"}
          </button>
        </form>
      </section>

      <footer className="login-page__footer">Quant Foundry · Self-hosted</footer>
    </main>
  );
}
