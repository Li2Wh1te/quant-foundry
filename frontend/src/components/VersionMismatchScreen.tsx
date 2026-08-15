import { RefreshCw, ShieldAlert } from "lucide-react";

import { useAuth } from "../auth/AuthContext";
import { FRONTEND_VERSION } from "../version";

export function VersionMismatchScreen() {
  const { backendVersion, logout } = useAuth();

  return (
    <main className="version-mismatch-screen">
      <section className="version-mismatch-card" aria-labelledby="version-mismatch-title">
        <ShieldAlert aria-hidden="true" />
        <h1 id="version-mismatch-title">前后端版本不匹配</h1>
        <p>为避免使用不兼容的管理接口，当前管理台已暂停加载。</p>
        <dl>
          <div>
            <dt>前端</dt>
            <dd>v{FRONTEND_VERSION}</dd>
          </div>
          <div>
            <dt>后端</dt>
            <dd>v{backendVersion ?? "未知"}</dd>
          </div>
        </dl>
        <div className="version-mismatch-card__actions">
          <button className="primary-button" type="button" onClick={() => window.location.reload()}>
            <RefreshCw aria-hidden="true" />
            刷新页面
          </button>
          <button className="secondary-button" type="button" onClick={logout}>
            退出登录
          </button>
        </div>
      </section>
    </main>
  );
}
