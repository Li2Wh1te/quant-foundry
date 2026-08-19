import {
  BookOpen,
  CalendarClock,
  CalendarDays,
  Code2,
  FileSearch,
  Landmark,
  LayoutDashboard,
  LineChart,
  LogOut,
  Search,
  X
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";
import { Brand } from "../components/Brand";
import { ThemeToggle } from "../components/ThemeToggle";
import { FRONTEND_VERSION } from "../version";

const QUICK_DESTINATIONS = [
  { to: "/admin", label: "数据运营总览", description: "查看当前数据与调度快照", icon: LayoutDashboard, shortcut: "⌘ 1" },
  { to: "/admin/data/etf-basics", label: "ETF 基础信息", description: "查询 ETF 标识与上市状态", icon: Landmark, shortcut: "⌘ 2" },
  { to: "/admin/data/trading-calendar", label: "交易日历", description: "查看交易日覆盖与检查点", icon: CalendarDays, shortcut: "⌘ 3" },
  { to: "/admin/tasks", label: "任务调度", description: "管理计划与查看最近执行", icon: CalendarClock, shortcut: "⌘ 4" },
  { to: "/admin/logs", label: "运行日志", description: "按中文事件摘要定位问题", icon: FileSearch, shortcut: "⌘ 5" },
  { to: "/admin/strategies", label: "策略工作台", description: "编写、校验和发布私有策略", icon: Code2, shortcut: "⌘ 6" },
  { to: "/admin/strategy-data", label: "策略数据接口", description: "查看策略可调用的 ETF 数据接口", icon: BookOpen, shortcut: "⌘ 7" }
] as const;

function pageTitle(pathname: string): string {
  if (pathname === "/admin") return "数据运营总览";
  if (pathname === "/admin/tasks") return "任务调度";
  if (pathname === "/admin/logs") return "运行日志";
  if (pathname.startsWith("/admin/data/etf-basics/")) return "ETF 详情";
  if (pathname === "/admin/data/etf-basics") return "ETF 基础信息";
  if (pathname === "/admin/data/trading-calendar") return "交易日历";
  if (pathname === "/admin/data/daily-quotes") return "日线行情";
  if (pathname === "/admin/strategies" || pathname.startsWith("/admin/strategies/")) return "策略工作台";
  if (pathname === "/admin/strategy-data") return "策略数据接口";
  return "管理工作区";
}

export function AdminPage({ children }: { children?: React.ReactNode }) {
  const { logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [commandOpen, setCommandOpen] = useState(false);
  const [commandQuery, setCommandQuery] = useState("");
  const commandInputRef = useRef<HTMLInputElement>(null);
  const currentTitle = pageTitle(location.pathname);

  const visibleDestinations = useMemo(() => {
    const normalizedQuery = commandQuery.trim().toLowerCase();
    if (!normalizedQuery) return QUICK_DESTINATIONS;
    return QUICK_DESTINATIONS.filter((destination) => (
      `${destination.label} ${destination.description}`.toLowerCase().includes(normalizedQuery)
    ));
  }, [commandQuery]);

  /**
   * A command palette keeps route changes discoverable without adding a second
   * permanent toolbar. The keyboard shortcut is scoped to this authenticated
   * shell and Escape always returns focus to the current page context.
   */
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setCommandOpen(true);
      }
      if (event.key === "Escape") setCommandOpen(false);
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  useEffect(() => {
    if (!commandOpen) return;
    const frame = window.requestAnimationFrame(() => commandInputRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [commandOpen]);

  function handleLogout() {
    logout();
    navigate("/login", { replace: true });
  }

  function openDestination(to: string) {
    setCommandOpen(false);
    setCommandQuery("");
    navigate(to);
  }

  return (
    <div className="admin-shell">
      <aside className="sidebar">
        <Brand compact />

        <nav className="sidebar__nav" aria-label="管理导航">
          <span className="sidebar__label">工作台</span>
          <NavLink className={({ isActive }) => `nav-item${isActive ? " nav-item--active" : ""}`} to="/admin" end>
            <LayoutDashboard aria-hidden="true" />
            <span>总览</span>
          </NavLink>

          <span className="sidebar__label">数据资产</span>
          <NavLink className={({ isActive }) => `nav-item${isActive ? " nav-item--active" : ""}`} to="/admin/data/etf-basics">
            <Landmark aria-hidden="true" />
            <span>ETF 基础信息</span>
          </NavLink>
          <NavLink className={({ isActive }) => `nav-item${isActive ? " nav-item--active" : ""}`} to="/admin/data/trading-calendar">
            <CalendarDays aria-hidden="true" />
            <span>交易日历</span>
          </NavLink>
          <NavLink className={({ isActive }) => `nav-item${isActive ? " nav-item--active" : ""}`} to="/admin/data/daily-quotes">
            <LineChart aria-hidden="true" />
            <span>日线行情</span>
          </NavLink>

          <span className="sidebar__label">运行与审计</span>
          <NavLink className={({ isActive }) => `nav-item${isActive ? " nav-item--active" : ""}`} to="/admin/tasks">
            <CalendarClock aria-hidden="true" />
            <span>任务调度</span>
          </NavLink>
          <NavLink className={({ isActive }) => `nav-item${isActive ? " nav-item--active" : ""}`} to="/admin/logs">
            <FileSearch aria-hidden="true" />
            <span>运行日志</span>
          </NavLink>

          <span className="sidebar__label">策略研究</span>
          <NavLink className={({ isActive }) => `nav-item${isActive ? " nav-item--active" : ""}`} to="/admin/strategies">
            <Code2 aria-hidden="true" />
            <span>策略工作台</span>
          </NavLink>
          <NavLink className={({ isActive }) => `nav-item${isActive ? " nav-item--active" : ""}`} to="/admin/strategy-data">
            <BookOpen aria-hidden="true" />
            <span>策略数据接口</span>
          </NavLink>
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
          <span className="sidebar__version">BUILD · v{FRONTEND_VERSION}</span>
        </div>
      </aside>

      <main className="admin-main">
        <header className="admin-header">
          <div className="admin-header__title">
            <span>WORKBENCH</span>
            <h1>{currentTitle}</h1>
          </div>
          <div className="admin-header__actions">
            <button className="command-trigger" type="button" onClick={() => setCommandOpen(true)} aria-haspopup="dialog">
              <Search aria-hidden="true" />
              <span>快速跳转</span>
              <kbd>⌘ K</kbd>
            </button>
            <ThemeToggle />
          </div>
        </header>

        {children ?? <section className="admin-content" aria-labelledby="empty-state-title">
          <div className="empty-state">
            <div className="empty-state__graphic" aria-hidden="true"><span>QF</span></div>
            <h2 id="empty-state-title">管理工作区已就绪</h2>
            <p>从左侧导航选择数据、调度或审计能力。</p>
          </div>
        </section>}
      </main>

      {commandOpen && (
        <div className="command-palette-backdrop" role="presentation" onMouseDown={() => setCommandOpen(false)}>
          <section className="command-palette" role="dialog" aria-modal="true" aria-label="快速跳转" onMouseDown={(event) => event.stopPropagation()}>
            <div className="command-palette__search">
              <Search aria-hidden="true" />
              <input ref={commandInputRef} value={commandQuery} onChange={(event) => setCommandQuery(event.target.value)} placeholder="搜索页面或操作…" aria-label="搜索页面或操作" />
              <button type="button" onClick={() => setCommandOpen(false)} aria-label="关闭快速跳转"><X aria-hidden="true" /></button>
            </div>
            <span className="command-palette__label">快速跳转</span>
            <div className="command-palette__results">
              {visibleDestinations.length > 0 ? visibleDestinations.map((destination) => {
                const Icon = destination.icon;
                return <button type="button" key={destination.to} onClick={() => openDestination(destination.to)}>
                  <Icon aria-hidden="true" />
                  <span><strong>{destination.label}</strong><small>{destination.description}</small></span>
                  <kbd>{destination.shortcut}</kbd>
                </button>;
              }) : <p>没有匹配的页面。</p>}
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
