import { Eye, EyeOff, LockKeyhole } from "lucide-react";
import { FormEvent, useRef, useState } from "react";
import { Navigate } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { LoadingScreen } from "../components/LoadingScreen";
import { VersionMismatchScreen } from "../components/VersionMismatchScreen";
import styles from "./LoginPage.module.css";

export function LoginPage() {
  const { status, login } = useAuth();
  const [token, setToken] = useState("");
  const [showToken, setShowToken] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const tokenInput = useRef<HTMLInputElement>(null);
  const requestPending = useRef(false);

  if (status === "checking") {
    return <LoadingScreen />;
  }
  if (status === "authenticated") {
    return <Navigate to="/admin" replace />;
  }
  if (status === "version_mismatch") {
    return <VersionMismatchScreen />;
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    // Guard synchronously because two submit events can arrive before React
    // commits the disabled state. Keep all validation next to the Token field.
    if (requestPending.current) return;
    setError(null);
    if (!token.trim()) {
      setError("请输入 API Token。");
      tokenInput.current?.focus();
      return;
    }

    requestPending.current = true;
    setSubmitting(true);
    try {
      // AuthProvider owns authentication, storage, and version compatibility.
      // Redirect only when it reports authenticated, preserving mismatch UI.
      await login(token);
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "登录失败，请稍后重试。"
      );
      tokenInput.current?.focus();
    } finally {
      requestPending.current = false;
      setSubmitting(false);
    }
  }

  return (
    <div className={styles.page}>
      <header className={styles.brandbar}>
        <a className={styles.brand} href="/login" aria-label="Quant Foundry">
          <span className={styles.brandMark} aria-hidden="true"><span>QF</span></span>
          <span className={styles.brandName}>Quant Foundry</span>
        </a>
        <div className={styles.deployment}>self-hosted · private instance</div>
      </header>

      <main className={styles.main}>
        <section className={styles.authFrame} aria-labelledby="login-title">
          <aside className={styles.brandPanel} aria-label="Quant Foundry 品牌区">
            <span className={styles.cornerTop} aria-hidden="true" />
            <span className={styles.cornerBottom} aria-hidden="true" />
            <div>
              <div className={styles.serial}>QF / ADMIN ACCESS</div>
              <h2>从数据到策略，保持同一条研究链路</h2>
              <p>连接数据源、运行采集任务、开展研究，并追踪每一次策略执行与系统记录。</p>
            </div>
            <div className={styles.panelBottom} aria-hidden="true">
              <div>QUANT RESEARCH SYSTEM</div>
              <div>SELF-HOSTED / PRIVATE</div>
            </div>
          </aside>
          <div className={styles.authPanel}>
            <div className={styles.authBox}>
              <div className={styles.eyebrow}>管理入口</div>
              <h1 id="login-title">登录 Quant Foundry</h1>
              <p className={styles.intro}>使用部署时生成的 API Token 登录管理控制台。</p>
              <form onSubmit={handleSubmit} noValidate aria-busy={submitting}>
                <div className={styles.field}>
                  <div className={styles.fieldHead}>
                    <label htmlFor="api-token">API Token</label>
                    <span className={styles.fieldMeta}>必填</span>
                  </div>
                  <div className={`${styles.inputWrap}${error ? ` ${styles.invalid}` : ""}`}>
                    <input
                      ref={tokenInput}
                      className={styles.tokenInput}
                      id="api-token"
                      name="token"
                      type={showToken ? "text" : "password"}
                      value={token}
                      onChange={(event) => { setToken(event.target.value); setError(null); }}
                      placeholder="输入 API Token"
                      autoComplete="current-password"
                      autoCapitalize="none"
                      spellCheck={false}
                      readOnly={submitting}
                      aria-required="true"
                      aria-invalid={Boolean(error)}
                      aria-describedby="token-help token-feedback"
                    />
                    <button
                      className={styles.eyeButton}
                      type="button"
                      onClick={() => { setShowToken((visible) => !visible); tokenInput.current?.focus(); }}
                      aria-label={showToken ? "隐藏 Token" : "显示 Token"}
                      aria-pressed={showToken}
                      title={showToken ? "隐藏 Token" : "显示 Token"}
                    >
                      {showToken ? <EyeOff aria-hidden="true" /> : <Eye aria-hidden="true" />}
                    </button>
                  </div>

                  <div className={styles.help} id="token-help">Token 由 Quant Foundry 部署环境生成。</div>
                  <div className={error ? styles.feedback : undefined} id="token-feedback" role="status" aria-live="polite">
                    {error}
                  </div>
                </div>

                <button
                  className={styles.submit}
                  type="submit"
                  disabled={submitting}
                >
                  {submitting && <span className={styles.spinner} aria-hidden="true" />}
                  <span>{submitting ? "正在登录" : "登录"}</span>
                </button>
                <div className={styles.keyboardNote}>按 <kbd>Enter</kbd> 提交</div>
              </form>
              <div className={styles.securityNote}>
                <span className={styles.securityIcon} aria-hidden="true"><LockKeyhole /></span>
                <span>API Token 相当于管理凭证。请勿分享给他人或粘贴到不受信任的页面。</span>
              </div>
            </div>
          </div>
        </section>
      </main>

      <footer className={styles.footer}>
        <span>Quant Foundry · Private Instance</span>
        <span className={styles.footerDot} aria-hidden="true" />
        <span className={styles.footerMono}>QF / ACCESS</span>
      </footer>
    </div>
  );
}
