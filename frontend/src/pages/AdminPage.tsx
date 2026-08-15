import { BookOpen, CalendarClock, Database, FileSearch, Grid2X2, LineChart, LogOut } from "lucide-react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { Brand } from "../components/Brand";
import { ThemeToggle } from "../components/ThemeToggle";
import { FRONTEND_VERSION } from "../version";

export function AdminPage({ children }: { children?: React.ReactNode }) {
  const { logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const dataCollectionActive = location.pathname.startsWith("/admin/data/");

  function handleLogout() {
    logout();
    navigate("/login", { replace: true });
  }

  return (
    <div className="admin-shell">
      <aside className="sidebar">
        <Brand compact />

        <nav className="sidebar__nav" aria-label="管理导航">
          <span className="sidebar__label">工作区</span>
          <NavLink className={({ isActive }) => `nav-item${isActive ? " nav-item--active" : ""}`} to="/admin" end>
            <Grid2X2 aria-hidden="true" />
            <span>概览</span>
          </NavLink>
          <NavLink className={({ isActive }) => `nav-item${isActive ? " nav-item--active" : ""}`} to="/admin/logs">
            <FileSearch aria-hidden="true" />
            <span>日志查询</span>
          </NavLink>
          <NavLink className={({ isActive }) => `nav-item${isActive ? " nav-item--active" : ""}`} to="/admin/tasks">
            <CalendarClock aria-hidden="true" />
            <span>任务调度</span>
          </NavLink>
          <div className={`nav-item nav-item--group${dataCollectionActive ? " nav-item--active" : ""}`}>
            <Database aria-hidden="true" />
            <span>数据采集</span>
          </div>
          <div className="nav-submenu" aria-label="数据采集页面">
            <NavLink className={({ isActive }) => `nav-submenu__item${isActive ? " nav-submenu__item--active" : ""}`} to="/admin/data/trading-calendar">
              <CalendarClock aria-hidden="true" /><span>交易日历</span>
            </NavLink>
            <NavLink className={({ isActive }) => `nav-submenu__item${isActive ? " nav-submenu__item--active" : ""}`} to="/admin/data/daily-quotes">
              <LineChart aria-hidden="true" /><span>日线行情</span>
            </NavLink>
          </div>
        </nav>

        <div className="sidebar__footer">
          <a className="nav-item" href="/docs" target="_blank" rel="noreferrer">
            <BookOpen aria-hidden="true" />
            <span>API 文档</span>
          </a>
          <button className="nav-item" type="button" onClick={handleLogout}>
            <LogOut aria-hidden="true" />
            <span>退出登录</span>
          </button>
          <span className="sidebar__version">v{FRONTEND_VERSION}</span>
        </div>
      </aside>

      <main className="admin-main">
        <header className="admin-header">
          <h1>管理控制台</h1>
          <div className="admin-header__actions">
            <span className="service-health">
              <i aria-hidden="true" />
              <span>服务运行正常</span>
            </span>
            <ThemeToggle />
          </div>
        </header>

        {children ?? <section className="admin-content" aria-labelledby="empty-state-title">
          <div className="empty-state">
            <div className="empty-state__graphic" aria-hidden="true">
              <span>QF</span>
            </div>
            <h2 id="empty-state-title">管理空间已就绪</h2>
            <p>当前版本尚未配置管理模块。后续能力会出现在左侧导航中。</p>
            <a className="secondary-button" href="/docs" target="_blank" rel="noreferrer">
              <BookOpen aria-hidden="true" />
              查看 API 文档
            </a>
          </div>
        </section>}
      </main>
    </div>
  );
}
