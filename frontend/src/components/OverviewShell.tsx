import { BookOpen, ChartNoAxesColumn, Clock3, Code2, Database, FileSearch, LayoutDashboard, LineChart, LogOut, Menu, Search, WalletCards, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { FRONTEND_VERSION } from "../version";
import "./Overview.css";

const groups = [
  { label: "工作台", items: [{ label: "总览", to: "/admin", icon: LayoutDashboard }] },
  { label: "数据采集", items: [
    { label: "数据源", to: "/admin/data-sources", icon: Database },
    { label: "采集任务", to: "/admin/tasks", icon: Clock3 }
  ] },
  { label: "市场数据", items: [{ label: "A 股市场", to: "/admin/data/etf-basics", icon: ChartNoAxesColumn }] },
  { label: "策略研究", items: [
    { label: "策略工作台", to: "/admin/strategies", icon: Code2 },
    { label: "策略数据接口", to: "/admin/strategy-data", icon: BookOpen },
    { label: "回测工作台", to: "/admin/backtest-runs", icon: ChartNoAxesColumn },
    { label: "回测账户", to: "/admin/backtest-accounts", icon: WalletCards }
  ] }
];
const toolItems = [
  { label: "运行日志", to: "/admin/logs", icon: FileSearch },
  { label: "日线行情", to: "/admin/data/daily-quotes", icon: LineChart },
  { label: "API 文档", to: "/docs", icon: Code2 },
  { label: "退出登录", to: "logout", icon: LogOut }
];
const destinations = [
  ...groups.flatMap(group => group.items.filter(item => item.to).map(item => ({ ...item, group: group.label }))),
  { label: "交易日历", to: "/admin/data/trading-calendar", group: "数据资产" },
  { label: "回测预检", to: "/admin/backtest-preflight", group: "策略研究" },
  ...toolItems.map(item => ({ ...item, group: "运行与工具" }))
];

/** Reviewed overview, source and collection-task pages share this shell. Legacy routes retain their
 * existing theme setting until each page is reviewed and accepted separately. */
export function OverviewShell({ children, title = "数据运营总览", section = "WORKBENCH", className = "" }: { children: React.ReactNode; title?: string; section?: string; className?: string }) {
  const { logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [collapsed, setCollapsed] = useState(() => window.innerWidth <= 1240);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const resultRef = useRef<HTMLDivElement>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const filtered = destinations.filter(item => `${item.label} ${item.group}`.toLowerCase().includes(query.trim().toLowerCase()));
  const close = useCallback(() => {
    setOpen(false);
    // The inert attribute is removed by the React commit before focus returns.
    requestAnimationFrame(() => returnFocus.current?.isConnected && returnFocus.current.focus());
  }, []);
  const show = useCallback(() => {
    returnFocus.current = document.activeElement as HTMLElement | null;
    setQuery(""); setSelected(0); setOpen(true);
  }, []);
  useEffect(() => {
    const onShortcut = (event: KeyboardEvent) => {
      if (document.querySelector("dialog:modal")) return;
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault(); if (open) close(); else show();
      }
    };
    window.addEventListener("keydown", onShortcut);
    return () => window.removeEventListener("keydown", onShortcut);
  }, [open, show, close]);
  useEffect(() => { if (open) inputRef.current?.focus(); }, [open]);
  useEffect(() => {
    if (open) resultRef.current?.children[selected]?.scrollIntoView({ block: "nearest" });
  }, [selected, open]);
  function go(to: string) {
    close();
    if (to === "logout") { logout(); navigate("/login", { replace: true }); }
    else if (to === "/docs") window.open("/docs", "_blank", "noopener,noreferrer");
    else navigate(to);
  }
  function navItem(item: typeof toolItems[number], tool = false) {
    const Icon = item.icon;
    const className = `qfo-nav-item${tool ? " qfo-tool-link" : ""}${item.to === location.pathname ? " qfo-active" : ""}${!item.to ? " qfo-nav-disabled" : ""}`;
    const label = item.to ? item.label : `${item.label}（尚未开放）`;
    const content = <><Icon aria-hidden="true" /><span>{item.label}</span></>;
    if (!item.to) return <button key={item.label} type="button" className={className} aria-disabled="true" aria-label={label} title={label}>{content}</button>;
    if (item.to === "logout") return <button key={item.label} type="button" className={className} aria-label={label} title={label} onClick={() => go(item.to)}>{content}</button>;
    if (item.to === "/docs") return <a key={item.label} className={className} aria-label={label} title={label} href="/docs" target="_blank" rel="noreferrer">{content}</a>;
    return <Link key={item.label} className={className} aria-label={label} title={label} to={item.to} aria-current={item.to === location.pathname ? "page" : undefined}>{content}</Link>;
  }
  return <div className={`qfo-root ${className}`}>
    <div className={`qfo-app${collapsed ? " qfo-sidebar-collapsed" : ""}`} inert={open}>
      <aside className="qfo-sidebar" aria-label="工作区侧栏">
        <div className="qfo-brand"><div className="qfo-mark">QF</div><div className="qfo-brand-copy"><div className="qfo-brand-name">Quant Foundry</div><div className="qfo-brand-meta">RESEARCH SYSTEM</div></div></div>
        <nav className="qfo-nav" id="overview-navigation" aria-label="主导航">{groups.map(group => <div className="qfo-nav-group" key={group.label}><div className="qfo-nav-label">{group.label}</div>{group.items.map(item => navItem(item))}</div>)}</nav>
        <div className="qfo-side-bottom"><nav className="qfo-nav-group" aria-label="运行与工具"><div className="qfo-nav-label">运行与工具</div>{toolItems.map(item => navItem(item, true))}</nav><div className="qfo-build">BUILD · v{FRONTEND_VERSION}</div></div>
      </aside>
      <section className="qfo-shell">
        <header className="qfo-topbar">
          <button className="qfo-icon-btn qfo-sidebar-toggle" type="button" aria-label={collapsed ? "展开侧栏" : "收起侧栏"} title={collapsed ? "展开侧栏" : "收起侧栏"} aria-expanded={!collapsed} aria-controls="overview-navigation" onClick={() => setCollapsed(value => !value)}><Menu aria-hidden="true" /></button>
          <div className="qfo-crumb"><span className="qfo-crumb-index">{section}</span><span className="qfo-crumb-sep">/</span><span className="qfo-crumb-title">{title}</span></div>
          <div className="qfo-top-actions"><button className="qfo-quick" type="button" aria-label="快速跳转" aria-haspopup="dialog" onClick={show}><Search aria-hidden="true" /><span>快速跳转</span><span className="qfo-kbd">⌘ K</span></button></div>
        </header>
        <main className="qfo-main">{children}</main>
      </section>
    </div>
    {open && <div className="qfo-command-backdrop qfo-open" onMouseDown={event => { if (event.target === event.currentTarget) close(); }}>
      <div className="qfo-command" ref={dialogRef} role="dialog" aria-modal="true" aria-label="快速跳转" onKeyDown={event => {
        if (event.key === "Escape") { event.preventDefault(); close(); }
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault(); inputRef.current?.focus();
          setSelected(value => (value + (event.key === "ArrowDown" ? 1 : -1) + filtered.length) % Math.max(1, filtered.length));
        }
        if (event.key === "Enter" && event.target === inputRef.current && filtered[selected]) { event.preventDefault(); go(filtered[selected].to); }
        if (event.key === "Tab") {
          const controls = [...(dialogRef.current?.querySelectorAll<HTMLElement>("input,button,a[href]") ?? [])];
          const next = controls.indexOf(document.activeElement as HTMLElement) + (event.shiftKey ? -1 : 1);
          if (next < 0 || next >= controls.length) { event.preventDefault(); controls[(next + controls.length) % controls.length]?.focus(); }
        }
      }}>
        <div className="qfo-command-search"><Search aria-hidden="true" /><input ref={inputRef} value={query} onChange={event => { setQuery(event.target.value); setSelected(0); }} aria-label="搜索页面或功能" placeholder="搜索页面或功能" autoComplete="off" role="combobox" aria-expanded="true" aria-controls="overview-search-results" aria-activedescendant={filtered[selected] ? `overview-destination-${selected}` : undefined} /><button className="qfo-icon-btn" type="button" aria-label="关闭快速跳转" onClick={close}><X aria-hidden="true" /></button></div>
        <div className="qfo-command-results" ref={resultRef} id="overview-search-results" role="listbox" aria-label="页面">{filtered.map((item, index) => <button className={`qfo-command-item${index === selected ? " qfo-selected" : ""}`} type="button" key={item.to} id={`overview-destination-${index}`} role="option" aria-selected={index === selected} onFocus={() => setSelected(index)} onClick={() => go(item.to)}><span>{item.label}</span><span>{item.group}</span></button>)}{!filtered.length && <p className="qfo-command-empty" role="status">没有匹配的页面</p>}</div>
      </div>
    </div>}
  </div>;
}
