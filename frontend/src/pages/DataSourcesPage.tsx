import { CircleAlert, Database, LoaderCircle, RefreshCw, Settings2, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { DataSourceApiError, listDataSources, readDataSource, saveDataSource, setDataSourceEnabled,
  sourceDraft, sourceState, testDataSource, validateSourceDraft } from "../api/dataSources";
import type { DataSource, SourceDraft, SourceProbe } from "../api/dataSources";
import { useAuth } from "../auth/AuthContext";
import { RUN_LABELS } from "../components/overviewPresentation";
import "./DataSources.css";

export function sourceTime(value: string | null): string {
  if (!value) return "尚未检查";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "时间未知" : new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false
  }).format(date);
}
const sourceMark = (source: DataSource) => source.key === "tushare" ? "TS" : source.name.slice(0, 2).toUpperCase();

/** Native modal dialogs make the whole shell inert and handle focus trapping,
 * including background navigation. Drafts live only in this mounted component. */
function ConnectionDialog({ source, onClose, onSaved, onUnauthorized }: {
  source: DataSource; onClose: () => void; onSaved: (source: DataSource) => void; onUnauthorized: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const form = useRef<HTMLFormElement>(null);
  const errorSummary = useRef<HTMLDivElement>(null);
  const pending = useRef(false);
  const [current, setCurrent] = useState(source);
  const [draft, setDraft] = useState(() => sourceDraft(source));
  const [busy, setBusy] = useState<"test" | "save" | "reload" | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [probe, setProbe] = useState<SourceProbe | null>(null);
  const [needsRefresh, setNeedsRefresh] = useState(false);
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    form.current?.querySelector<HTMLInputElement>("input")?.focus();
    return () => element?.close();
  }, []);

  const focusError = () => requestAnimationFrame(() => errorSummary.current?.focus());
  function update(key: string, value: SourceDraft[string]) {
    setDraft(previous => ({ ...previous, [key]: value }));
    setErrors(previous => { const next = { ...previous }; delete next[key]; return next; });
    // A successful probe applies only to the exact submitted draft.
    setProbe(null); setError("");
  }
  async function submit(mode: "test" | "save") {
    if (pending.current || needsRefresh) return;
    const validation = validateSourceDraft(current, draft);
    setErrors(validation); setProbe(null); setError("");
    if (Object.keys(validation).length) { setError("请检查以下配置项。"); focusError(); return; }
    pending.current = true; setBusy(mode);
    try {
      if (mode === "test") setProbe(await testDataSource(current, draft));
      else onSaved(await saveDataSource(current, draft));
    } catch (caught) {
      if (caught instanceof DataSourceApiError && caught.status === 401) { onUnauthorized(); return; }
      setError(caught instanceof DataSourceApiError ? caught.message : "操作未完成，请稍后重试。");
      if (caught instanceof DataSourceApiError && caught.field) setErrors({ [caught.field]: caught.message });
      setNeedsRefresh(caught instanceof DataSourceApiError && (caught.status === 409 || caught.status === 0));
      focusError();
    } finally { pending.current = false; setBusy(null); }
  }
  async function reload() {
    if (pending.current) return;
    pending.current = true; setBusy("reload");
    try {
      const fresh = await readDataSource(current.key);
      setCurrent(fresh); setDraft(sourceDraft(fresh)); setNeedsRefresh(false); setError(""); setErrors({}); setProbe(null);
    } catch (caught) {
      if (caught instanceof DataSourceApiError && caught.status === 401) onUnauthorized();
      else { setError("重新加载失败，请稍后重试。"); focusError(); }
    } finally { pending.current = false; setBusy(null); }
  }
  return <dialog ref={dialog} className="qfs-dialog" aria-labelledby="source-dialog-title"
    onCancel={event => { event.preventDefault(); if (!pending.current) onClose(); }}
    onClick={event => {
      if (event.target !== event.currentTarget || pending.current) return;
      const rect = event.currentTarget.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) onClose();
    }}>
    <form ref={form} noValidate onSubmit={event => { event.preventDefault(); void submit("save"); }}>
      <header className="qfs-dialog-head"><h2 id="source-dialog-title">配置 {current.name} 连接</h2><button className="qfo-icon-btn" type="button" aria-label="关闭连接配置" disabled={!!busy} onClick={onClose}><X aria-hidden="true" /></button></header>
      <div className="qfs-dialog-body">
        {error && <div ref={errorSummary} className="qfs-message qfs-error" tabIndex={-1} role="alert"><strong>{error}</strong>{Object.entries(errors).length > 0 && <ul>{Object.entries(errors).map(([key, message]) => <li key={key}><a href={`#source-field-${key}`}>{message}</a></li>)}</ul>}
          {needsRefresh && <><p>重新加载会清空当前草稿，并读取已保存的配置。</p><button type="button" className="qfo-secondary-btn" disabled={!!busy} onClick={() => void reload()}>重新加载配置</button></>}
        </div>}
        <fieldset disabled={!!busy || needsRefresh} className="qfs-fields">
          {current.fields.map(field => {
            const id = `source-field-${field.key}`;
            const configured = current.secret_fields_configured.includes(field.key);
            return <div className="qfs-field" key={field.key}>
              <label htmlFor={id}>{field.label}{field.required && !(field.type === "secret" && configured) && <span className="qfs-required"> *</span>}</label>
              {field.type === "boolean" ? <input id={id} type="checkbox" checked={!!draft[field.key]} onChange={event => update(field.key, event.target.checked)} />
                : <input id={id} type={field.type === "secret" ? "password" : field.type === "url" ? "url" : ["integer", "number"].includes(field.type) ? "number" : "text"}
                  value={String(draft[field.key] ?? "")} autoComplete={field.type === "secret" ? "new-password" : "off"} spellCheck={false}
                  step={field.type === "integer" ? 1 : "any"} maxLength={field.type === "secret" ? 4096 : 2048}
                  aria-invalid={!!errors[field.key]} aria-describedby={`${id}-help${errors[field.key] ? ` ${id}-error` : ""}`}
                  placeholder={field.type === "secret" && configured ? "已配置，留空沿用现有凭据" : undefined}
                  onChange={event => update(field.key, event.target.value === "" ? "" : ["integer", "number"].includes(field.type) ? Number(event.target.value) : event.target.value)} />}
              <p id={`${id}-help`} className="qfs-field-help">{field.help}</p>
              {errors[field.key] && <p className="qfs-field-error" id={`${id}-error`}>{errors[field.key]}</p>}
            </div>;
          })}
        </fieldset>
        <p className="qfs-form-note">测试连接仅验证当前表单，不保存。保存时先验证，通过后才生效；失败保留原配置。</p>
        {probe && <div className={`qfs-message ${probe.ok ? "qfs-success" : "qfs-error"}`} role="status"><strong>{probe.ok ? "测试通过 · 尚未保存" : "连接测试未通过"}</strong><p>{probe.message}</p></div>}
        <span className="qfs-sr-only" role="status">{busy === "test" ? "正在测试连接" : busy === "save" ? "正在验证并保存配置" : ""}</span>
      </div>
      <footer className="qfs-dialog-foot"><button className="qfo-secondary-btn" type="button" disabled={!!busy || needsRefresh} onClick={() => void submit("test")}>{busy === "test" && <LoaderCircle className="spin" aria-hidden="true" />}{busy === "test" ? "测试中…" : "测试连接"}</button><button className="qfo-primary-btn" type="submit" disabled={!!busy || needsRefresh}>{busy === "save" && <LoaderCircle className="spin" aria-hidden="true" />}{busy === "save" ? "验证并保存中…" : "保存配置"}</button></footer>
    </form>
  </dialog>;
}

export function SourceDetail({ source, busy, onConfigure, onToggle }: {
  source: DataSource; busy: boolean; onConfigure: () => void; onToggle: () => void;
}) {
  const state = sourceState(source);
  const publicFields = source.fields.filter(field => field.type !== "secret");
  return <section className="qfs-sheet qfs-detail" aria-label={`${source.name} 数据源详情`}>
    <header className="qfs-detail-head"><div className="qfs-identity"><div className="qfs-mark">{sourceMark(source)}</div><div><div className="qfs-name-row"><h2>{source.name}</h2><span className={`qfs-status qfs-${state.tone}`}><i />{state.label}</span></div><p>结构化市场与基础数据接口</p></div></div>
      {/* Keep labels and native button appearance stable during short requests.
       * Aria-disabled preserves focus; guarded handlers still block interaction. */}
      <div className="qfs-actions"><button className="qfo-secondary-btn" type="button" onClick={() => { if (!busy) onConfigure(); }} aria-disabled={busy}><Settings2 aria-hidden="true" />配置连接</button><label className="qfs-switch-wrap"><span>启用</span><button className={`qfs-switch${source.enabled ? " qfs-on" : ""}`} type="button" role="switch" aria-label={`启用 ${source.name} 数据源`} aria-checked={source.enabled} aria-disabled={busy} aria-busy={busy} disabled={!source.configured && !source.enabled} onClick={() => { if (!busy) onToggle(); }} title="停用会跳过等待运行；已开始的任务继续执行，任务计划保留。" /></label></div>
    </header>
    <div className="qfs-connection-band">
      <div className="qfs-connection-item">{publicFields.map(field => <div key={field.key}><div className="qfs-connection-label">{field.label}</div><div className="qfs-connection-value qfs-mono" title={String(source.values[field.key] ?? "未配置")}>{String(source.values[field.key] ?? "未配置")}</div></div>)}<p>当前连接配置</p></div>
      <div className="qfs-connection-item"><div className="qfs-connection-label">认证</div><div className="qfs-connection-value">{source.configured ? "凭据已配置" : "尚未配置"}</div><p>凭据不会在页面中明文显示</p></div>
      <div className="qfs-connection-item"><div className="qfs-connection-label">最近检查</div><div className="qfs-connection-value qfs-mono">{sourceTime(source.checked_at)}</div><p>保存配置时检查 · 上海时间</p></div>
      <div className="qfs-connection-item"><div className="qfs-connection-label">状态</div><span className={`qfs-state qfs-${state.tone}`}><i />{state.label}</span><p>{!source.enabled ? "已开始的任务继续执行" : !source.configured ? "请先配置连接" : source.check_status === "not_checked" ? "已配置不代表连接可用" : "最近检查结果，非实时状态"}</p></div>
    </div>
    <div className="qfs-scripts-head"><h3>采集脚本</h3><span>{source.capabilities.length} 个已注册脚本</span><small>从脚本创建采集任务</small></div>
    <div className="qfs-table-wrap" tabIndex={0} role="region" aria-label="采集脚本表格，可横向和纵向滚动">
      <table className="qfs-table"><colgroup><col style={{width:"43%"}} /><col style={{width:"11%"}} /><col style={{width:"13%"}} /><col style={{width:"19%"}} /><col style={{width:"14%"}} /></colgroup><thead><tr><th scope="col">脚本</th><th scope="col">关联任务</th><th scope="col">状态</th><th scope="col">最近运行</th><th scope="col"><span className="qfs-sr-only">操作</span></th></tr></thead>
        <tbody>{source.capabilities.map(item => <tr key={item.key}>
          <td><div className="qfs-script-name">{item.name}<span className="qfs-script-english">（{item.english_name}）</span></div></td>
          <td><span className="qfs-mono">{item.task_count}</span> 个</td>
          <td><span className={`qfs-state ${item.available ? "qfs-ok" : "qfs-neutral"}`}><i />{item.available ? "就绪" : source.enabled ? "待配置" : "已停用"}</span></td>
          <td>{item.last_run ? <><div className="qfs-mono">{sourceTime(item.last_run.finished_at ?? item.last_run.started_at ?? item.last_run.created_at)}</div><span className="qfs-run-status">{RUN_LABELS[item.last_run.status] ?? "状态未知"}</span></> : <span className="qfs-muted">暂无运行</span>}</td>
          <td><Link className="qfs-task-link" aria-label={`配置任务：${item.name}（${item.english_name}）`} to={`/admin/tasks?create_type=${encodeURIComponent(item.key)}`}>配置任务</Link></td>
        </tr>)}</tbody>
      </table>{!source.capabilities.length && <p className="qfs-empty">此数据源尚无已注册的采集脚本。</p>}
    </div>
  </section>;
}

export function DataSourcesPage() {
  const { logout } = useAuth(); const navigate = useNavigate();
  const [sources, setSources] = useState<DataSource[] | null>(null);
  const [selected, setSelected] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  useEffect(() => {
    if (!message) return;
    // Successful feedback is transient and never participates in page layout.
    // Cancel the previous expiry when another action replaces the message.
    const timer = window.setTimeout(() => setMessage(""), 5000);
    return () => window.clearTimeout(timer);
  }, [message]);
  const [editing, setEditing] = useState<DataSource | null>(null);
  const [changing, setChanging] = useState(false);
  const [stale, setStale] = useState(false);
  const pending = useRef(false);
  const request = useRef<AbortController | null>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const source = sources?.find(item => item.key === selected) ?? sources?.[0];
  const unauthorized = useCallback(() => { logout(); navigate("/login", { replace: true }); }, [logout, navigate]);
  const load = useCallback(async () => {
    request.current?.abort(); const controller = new AbortController(); request.current = controller;
    setLoading(true); setError(""); setMessage("");
    try {
      const result = await listDataSources(controller.signal);
      if (!controller.signal.aborted) { setSources(result.items); setStale(false); }
    } catch (caught) {
      if (controller.signal.aborted) return;
      if (caught instanceof DataSourceApiError && caught.status === 401) unauthorized();
      else { setStale(true); setError(caught instanceof DataSourceApiError ? caught.message : "数据源加载失败。"); }
    } finally { if (!controller.signal.aborted) setLoading(false); }
  }, [unauthorized]);
  useEffect(() => { void load(); return () => request.current?.abort(); }, [load]);
  function replace(updated: DataSource) { setSources(previous => previous?.map(item => item.key === updated.key ? updated : item) ?? [updated]); }
  function close() { setEditing(null); requestAnimationFrame(() => returnFocus.current?.isConnected && returnFocus.current.focus()); }
  async function toggle() {
    if (!source || pending.current || stale || loading) return;
    pending.current = true; setChanging(true); setError(""); setMessage("");
    try {
      const result = await setDataSourceEnabled(source, !source.enabled); replace(result);
      setMessage(result.enabled ? `${result.name} 已启用，后续计划正常执行，不补跑停用期间的计划。`
        : `${result.name} 已停用，已跳过 ${result.skipped_runs ?? 0} 个等待运行；已开始的任务继续执行，任务计划保留。`);
    } catch (caught) {
      if (caught instanceof DataSourceApiError && caught.status === 401) unauthorized();
      else { setStale(true); setError(caught instanceof DataSourceApiError ? caught.message : "状态更新未完成，请刷新后核对。"); }
    } finally { pending.current = false; setChanging(false); }
  }
  return <div className={`qfo-content qfs-content${changing ? " qfs-updating" : ""}`}>
    <div className="qfo-page-head"><div><div className="qfo-eyebrow">Data Sources</div><h1>数据源</h1><p className="qfo-page-desc">查看连接状态、维护连接配置，并管理各数据源可用的采集脚本。</p></div><div className="qfs-page-actions"><span className="qfs-head-status">{sources ? `${sources.filter(item => item.configured).length} / ${sources.length} 个数据源已配置` : "正在读取数据源"}</span><button type="button" className="qfo-icon-btn" aria-label="刷新数据源" title="刷新数据源" disabled={loading || changing || !!editing} onClick={() => void load()}><RefreshCw className={loading ? "spin" : ""} aria-hidden="true" /></button></div></div>
    {error && <div className="qfs-message qfs-error" role="alert"><CircleAlert aria-hidden="true" /><span>{error}{sources && " 当前显示上次读取的状态，刷新成功后可继续操作。"}</span><button className="qfo-secondary-btn" type="button" disabled={loading} onClick={() => void load()}>重试</button></div>}
    <div className={`qfo-toast qfs-toast${message ? " qfo-show" : ""}`} role="status" aria-live="polite" aria-atomic="true">{message}</div>
    {!sources ? <div className="qfs-sheet qfs-empty" role="status">{loading ? <><LoaderCircle className="spin" aria-hidden="true" />正在加载数据源…</> : "未能读取数据源，请重试。"}</div>
      : sources.length === 0 ? <div className="qfs-sheet qfs-empty"><Database aria-hidden="true" /><p>暂无已接入的数据源。</p></div>
      : <div className="qfs-layout"><section className="qfs-sheet qfs-list" aria-label="已接入数据源"><header className="qfs-sheet-head"><h2>已接入</h2><span>{sources.length} 个数据源</span></header><div className="qfs-list-body">{sources.map(item => { const state = sourceState(item); return <button type="button" key={item.key} className={`qfs-option${source?.key === item.key ? " qfs-selected" : ""}`} aria-pressed={source?.key === item.key} disabled={changing} onClick={() => { setSelected(item.key); setMessage(""); }}><span className="qfs-mark">{sourceMark(item)}</span><span><strong>{item.name}</strong><small>{item.capabilities.length} 个采集脚本</small></span><span className={`qfs-state qfs-${state.tone}`}><i />{state.label}</span></button>; })}<p className="qfs-list-footer">维护已接入数据源的连接配置与启用状态。</p></div></section>
        {source && <SourceDetail source={source} busy={changing || loading || stale} onConfigure={() => { returnFocus.current = document.activeElement as HTMLElement; setEditing(source); }} onToggle={() => void toggle()} />}
      </div>}
    {editing && <ConnectionDialog source={editing} onClose={close} onUnauthorized={unauthorized} onSaved={updated => { replace(updated); close(); setMessage(`${updated.name} 连接已验证并保存。`); }} />}
  </div>;
}
