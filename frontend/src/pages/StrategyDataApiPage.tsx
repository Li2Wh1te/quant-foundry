import { Check, ChevronDown, Code2, Copy, Search } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { API_ENTRIES, API_GROUPS, DEFAULT_STATE, filterCatalog, restoreCatalogState, TABS, type CatalogState, type Property } from "./strategyData/catalog";
import "./strategyData/StrategyDataApi.css";

const STORAGE_KEY = "qf.strategy-data.workspace.v1";
function readWorkspace() {
  try { return JSON.parse(sessionStorage.getItem(STORAGE_KEY) || "null"); }
  catch { return null; }
}
function PropertyCard({ title, rows }: { title: string; rows: Property[] }) {
  return <section className="qfa-card"><header><h3>{title}</h3><span>{rows.length} 项</span></header><div className="qfa-card-body">
    {rows.length ? <dl className="qfa-properties">{rows.map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl> : <p className="qfa-no-params">无参数。</p>}
  </div></section>;
}

export function StrategyDataApiPage() {
  const [saved] = useState(readWorkspace);
  const [state, setState] = useState<CatalogState>(() => restoreCatalogState(saved?.state));
  const [menuOpen, setMenuOpen] = useState(false);
  const [notice, setNotice] = useState("");
  const [copying, setCopying] = useState(false);
  const filterRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const catalogRef = useRef<HTMLDivElement>(null);
  const detailRef = useRef<HTMLDivElement>(null);
  const mainRef = useRef<HTMLDivElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const copyPending = useRef(false);
  const mounted = useRef(true);
  const scroll = useRef<Record<string, number>>(saved?.scroll && typeof saved.scroll === "object" ? saved.scroll : {});
  const entry = API_ENTRIES.find(item => item.id === state.api) ?? API_ENTRIES[0];
  const visible = filterCatalog(state.query, state.type);
  const example = entry.examples[state.example] ?? entry.examples[0];
  const detailKey = `${entry.id}:${state.tab}`;
  const stateRef = useRef(state);
  stateRef.current = state;

  function save() {
    try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ state: stateRef.current, scroll: scroll.current })); }
    catch { /* Storage is optional; restricted browsers keep a usable in-memory workspace. */ }
  }
  function rememberScroll(key: string, value: number) { scroll.current[key] = value; save(); }
  function restoreScroll(key: string) {
    const value = scroll.current[key];
    return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : 0;
  }
  useEffect(() => { save(); }, [state]);
  useLayoutEffect(() => {
    if (detailRef.current) detailRef.current.scrollTop = restoreScroll(detailKey);
  }, [detailKey]);
  useLayoutEffect(() => {
    if (catalogRef.current) catalogRef.current.scrollTop = restoreScroll("catalog");
    const main = mainRef.current?.closest<HTMLElement>(".qfo-main");
    if (!main) return;
    main.scrollTop = restoreScroll("main");
    const onScroll = () => rememberScroll("main", main.scrollTop);
    main.addEventListener("scroll", onScroll, { passive: true });
    return () => main.removeEventListener("scroll", onScroll);
  }, []);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; if (timer.current) clearTimeout(timer.current); };
  }, []);
  useEffect(() => {
    if (!menuOpen) return;
    menuRef.current?.querySelector<HTMLElement>('[aria-checked="true"]')?.focus();
    const outside = (event: PointerEvent) => {
      if (!filterRef.current?.contains(event.target as Node)) setMenuOpen(false);
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [menuOpen]);

  async function copy(value: string, label: string) {
    // Bind feedback to the copied value, not to the selection when the promise
    // eventually resolves. A denied clipboard write must never report success.
    if (copyPending.current) return;
    copyPending.current = true; setCopying(true); setNotice("");
    if (timer.current) clearTimeout(timer.current);
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(value);
      if (mounted.current) setNotice(`${label}已复制`);
    } catch {
      if (mounted.current) setNotice("复制失败：请选中接口名称或代码手动复制，或检查浏览器剪贴板权限。");
    } finally {
      copyPending.current = false;
      if (mounted.current) { setCopying(false); timer.current = setTimeout(() => setNotice(""), 6000); }
    }
  }
  function chooseType(type: CatalogState["type"]) {
    setState(value => ({ ...value, type })); setMenuOpen(false); triggerRef.current?.focus();
  }
  return <div className="qfa-page" ref={mainRef}>
    <header className="qfa-page-head"><div><div className="qfa-eyebrow">Strategy Context API</div><h1>策略数据接口</h1><p>查阅策略运行时可访问的数据接口、时间边界、参数与返回结构。</p></div>
      <Link className="qfa-button" to="/admin/strategies"><Code2 aria-hidden="true" />返回策略工作台</Link>
    </header>
    <div className="qfa-workspace">
      <aside className="qfa-sheet qfa-catalog" aria-label="接口目录">
        <header className="qfa-sheet-head"><h2>接口目录</h2><span aria-live="polite">{visible.length} 项</span></header>
        <div className="qfa-search-row">
          <label className="qfa-search"><Search aria-hidden="true" /><input aria-label="搜索接口" placeholder="搜索接口" value={state.query} onChange={event => setState(value => ({ ...value, query: event.target.value }))} /></label>
          <div className="qfa-filter" ref={filterRef} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setMenuOpen(false); }} onKeyDown={event => {
            if (event.key === "Escape" && menuOpen) { event.preventDefault(); event.stopPropagation(); setMenuOpen(false); triggerRef.current?.focus(); }
          }}>
            <button className="qfa-type-trigger" ref={triggerRef} type="button" aria-haspopup="menu" aria-expanded={menuOpen} aria-controls="qfa-type-menu" onClick={() => setMenuOpen(value => !value)} onKeyDown={event => { if (event.key === "ArrowDown") { event.preventDefault(); setMenuOpen(true); } }}>
              {API_GROUPS.find(group => group.id === state.type)?.label ?? "全部类型"}<ChevronDown aria-hidden="true" />
            </button>
            {menuOpen && <div className="qfa-type-menu" id="qfa-type-menu" role="menu" aria-label="接口类型" ref={menuRef} onKeyDown={event => {
              const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>("button")];
              const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
              const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : event.key === "ArrowDown" ? (current + 1) % buttons.length : event.key === "ArrowUp" ? (current - 1 + buttons.length) % buttons.length : -1;
              if (next >= 0) { event.preventDefault(); buttons[next]?.focus(); }
            }}>
              {[{ id: "all" as const, label: "全部类型", index: "ALL" }, ...API_GROUPS].map(group => <button key={group.id} role="menuitemradio" aria-checked={state.type === group.id} type="button" onClick={() => chooseType(group.id)}><span>{group.label}</span>{state.type === group.id ? <Check aria-hidden="true" /> : <span className="qfa-menu-index">{group.index === "MARKET DATA" ? "DATA" : group.index}</span>}</button>)}
            </div>}
          </div>
        </div>
        <div className="qfa-catalog-scroll" ref={catalogRef} onScroll={event => rememberScroll("catalog", event.currentTarget.scrollTop)}>
          {API_GROUPS.map(group => {
            const items = visible.filter(item => item.group === group.id);
            return items.length > 0 && <section className="qfa-group" key={group.id} aria-label={group.label}><h3>{group.index}</h3>{items.map(item => <button className={`qfa-api-item${entry.id === item.id ? " qfa-active" : ""}`} type="button" key={item.id} aria-current={entry.id === item.id ? "true" : undefined} onClick={() => setState(value => ({ ...value, api: item.id, example: value.api === item.id ? value.example : 0 }))}>
              <span className="qfa-api-name">{item.signature}</span><span className="qfa-api-desc">{item.summary}</span><span className="qfa-badge">{item.badge}</span>
            </button>)}</section>;
          })}
          {!visible.length && <div className="qfa-empty" role="status"><p>没有匹配的接口。</p><button className="qfa-button" type="button" onClick={() => setState(value => ({ ...value, query: DEFAULT_STATE.query, type: DEFAULT_STATE.type }))}>清空搜索和筛选</button></div>}
        </div>
      </aside>
      <section className="qfa-sheet qfa-detail" aria-label="接口详情">
        <header className="qfa-detail-head"><div><h2>{entry.signature}</h2><p>{entry.summary}</p><div className="qfa-meta">{entry.meta.map(([key, value], index) => <span className={index === 0 ? "qfa-tag qfa-tag-green" : "qfa-tag"} key={key}>{key} · {value}</span>)}</div></div><button className="qfa-button" type="button" disabled={copying} onClick={() => void copy(entry.signature, entry.signature)}><Copy aria-hidden="true" />复制名称</button></header>
        <div className="qfa-tabs" role="tablist" aria-label="接口文档" onKeyDown={event => {
          const current = TABS.findIndex(tab => tab.id === state.tab);
          const next = event.key === "ArrowRight" ? (current + 1) % TABS.length : event.key === "ArrowLeft" ? (current + TABS.length - 1) % TABS.length : event.key === "Home" ? 0 : event.key === "End" ? TABS.length - 1 : -1;
          if (next >= 0) { event.preventDefault(); setState(value => ({ ...value, tab: TABS[next].id })); document.getElementById(`qfa-tab-${TABS[next].id}`)?.focus(); }
        }}>{TABS.map(tab => <button type="button" id={`qfa-tab-${tab.id}`} key={tab.id} role="tab" aria-selected={state.tab === tab.id} aria-controls="qfa-panel" tabIndex={state.tab === tab.id ? 0 : -1} onClick={() => setState(value => ({ ...value, tab: tab.id }))}>{tab.label}</button>)}</div>
        <div className="qfa-detail-body" id="qfa-panel" ref={detailRef} role="tabpanel" aria-labelledby={`qfa-tab-${state.tab}`} tabIndex={0} onScroll={event => rememberScroll(detailKey, event.currentTarget.scrollTop)}>
          {state.tab === "reference" && <div className="qfa-info-grid"><PropertyCard title="参数" rows={entry.params} /><PropertyCard title="返回值" rows={entry.returns} /><section className="qfa-card qfa-full"><header><h3>使用说明</h3></header><div className="qfa-card-body qfa-notes">{entry.notes.map(note => <p className="qfa-note" key={note}>{note}</p>)}</div></section></div>}
          {state.tab === "example" && <><div className="qfa-example-tabs" aria-label="调用示例">{entry.examples.map((item, index) => <button type="button" key={item.title} aria-pressed={state.example === index} onClick={() => setState(value => ({ ...value, example: index }))}>{item.title}</button>)}</div><div className="qfa-code-panel"><header><span>{example.title}</span><button type="button" disabled={copying} onClick={() => void copy(example.code, `${entry.signature} 示例代码`)}>复制代码</button></header><pre tabIndex={0} aria-label="Python 示例代码"><code>{example.code}</code></pre></div><p className="qfa-example-note">在策略决策函数中使用，context 由运行时提供。示例不保证当前数据覆盖完整；调用条件与限制见「说明」和「时间边界」。</p></>}
          {state.tab === "boundary" && <div className="qfa-info-grid"><PropertyCard title="可见性规则" rows={entry.boundary} /><section className="qfa-card"><header><h3>统一原则</h3></header><div className="qfa-card-body"><p className="qfa-note"><strong>查询不能超过当前策略可见截止时点。</strong><br />运行时执行时间边界与数据资格检查；查看交易日期或决策时间，不会改变这些限制。</p></div></section></div>}
        </div>
      </section>
    </div>
    <div className={`qfa-toast${notice ? " qfa-toast-visible" : ""}`} role="status" aria-live="polite" aria-atomic="true">{notice}</div>
  </div>;
}
