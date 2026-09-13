import { useEffect, useRef, useState, type ReactNode } from "react";
import { CircleAlert, Copy, LoaderCircle, Plus, RefreshCw, Search, X } from "lucide-react";
import { useAuth } from "../auth/AuthContext";
import { AccountProfileApiError, getAccountOverview, getAccountPage, getAccountUsage, type AccountOverview, type AccountPage, type AccountProfile, type AccountProfileStatus, type AccountUsage, type FeeRule } from "../api/accountProfiles";
import { AccountDeleteDialog } from "./accounts/AccountDeleteDialog";
import { AccountWizard } from "./accounts/AccountWizard";
import { CATEGORIES, LEVELS, META, MODES, STATUS, formatTime, sideLabel } from "./accounts/editor";
import "./accounts/Accounts.css";

const RUN_STATUS: Record<string, string> = { queued: "排队中", starting: "启动中", running: "运行中", cancel_requested: "取消中", succeeded: "已完成", failed: "失败", cancelled: "已取消", timed_out: "已超时", indeterminate: "结果待确认" };
function Badge({ status }: { status: AccountProfileStatus }) { return <span className={`qfac-badge qfac-badge-${status}`}>{STATUS[status]}</span>; }
function Card({ title, action, children }: { title: string; action?: ReactNode; children: ReactNode }) { return <section className="qfac-card"><header><h3>{title}</h3>{action}</header>{children}</section>; }
function Properties({ values }: { values: [string, ReactNode][] }) { return <dl className="qfac-properties">{values.map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value ?? "—"}</dd></div>)}</dl>; }
function Pager({ total, offset, limit, busy, onChange, label }: { total: number; offset: number; limit: number; busy: boolean; onChange: (value: number) => void; label: string }) {
  return <nav className="qfac-pager" aria-label={label}><span>{total ? `${offset + 1}–${Math.min(offset + limit, total)} / ${total}` : "0 项"}</span><button className="qfac-button" disabled={busy || !offset} onClick={() => onChange(Math.max(0, offset - limit))} aria-label={`${label}上一页`}>上一页</button><button className="qfac-button" disabled={busy || offset + limit >= total} onClick={() => onChange(offset + limit)} aria-label={`${label}下一页`}>下一页</button></nav>;
}
function FeeCard({ rule }: { rule: FeeRule }) {
  return <article className="qfac-card qfac-fee-card"><header><h3>{CATEGORIES[rule.category] ?? rule.category}</h3><code>{rule.key}</code><span className="qfac-tag">{sideLabel(rule.side)}</span></header><Properties values={[
    ["费率", rule.rate], ["最低费用", rule.minimum], ["固定费用", rule.fixed_amount], ["取整层级", rule.rounding_level ? LEVELS[rule.rounding_level] : "未配置"], ["取整范围", rule.rounding_scope], ["取整方式", rule.rounding_mode ? MODES[rule.rounding_mode] : "未配置"], ["取整精度", rule.rounding_precision],
  ]} /><div className="qfac-fee-conditions"><span>适用条件</span><p>{Object.keys(rule.applicability).length ? Object.entries(rule.applicability).map(([key, value]) => `${key} = ${JSON.stringify(value)}`).join("；") : "无属性限制"}</p></div></article>;
}
export function AccountProfilesPage() {
  const { logout } = useAuth();
  const [overview, setOverview] = useState<AccountOverview | null>(null), [statsError, setStatsError] = useState<string | null>(null);
  const [page, setPage] = useState<AccountPage | null>(null), [listError, setListError] = useState<string | null>(null);
  const [selected, setSelected] = useState<AccountProfile | null>(null);
  const [query, setQuery] = useState(""), [keyword, setKeyword] = useState("");
  const [status, setStatus] = useState<AccountProfileStatus | "">("");
  const [offset, setOffset] = useState(0), [revision, setRevision] = useState(0), [loading, setLoading] = useState(true), [statsLoading, setStatsLoading] = useState(true);
  const [usage, setUsage] = useState<AccountUsage | null>(null), [usageError, setUsageError] = useState<string | null>(null), [usageLoading, setUsageLoading] = useState(false), [usageOffset, setUsageOffset] = useState(0);
  const [tab, setTab] = useState("overview"), [notice, setNotice] = useState<string | null>(null);
  const [noticeSequence, setNoticeSequence] = useState(0);
  const usageScope = useRef("");
  const [editor, setEditor] = useState<{ source?: AccountProfile; copy: boolean } | null>(null);
  const [deleting, setDeleting] = useState<AccountProfile | null>(null);
  const preferred = useRef<string | null>(null);
  function failure(error: unknown): string {
    if (error instanceof AccountProfileApiError && error.status === 401) logout();
    return error instanceof Error ? error.message : "加载失败，请重试。";
  }
  // Success feedback floats above the workspace and expires independently of
  // reloads, so showing/dismissing it never changes the page's geometry.
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 4000);
    return () => window.clearTimeout(timer);
  }, [notice, noticeSequence]);
  useEffect(() => { const timer = window.setTimeout(() => { setKeyword(query); setOffset(0); }, 250); return () => clearTimeout(timer); }, [query]);
  useEffect(() => {
    const controller = new AbortController(); setStatsLoading(true); setStatsError(null);
    getAccountOverview(controller.signal).then(value => { if (!controller.signal.aborted) setOverview(value); }).catch(error => { if (!controller.signal.aborted) { setOverview(null); setStatsError(failure(error)); } }).finally(() => { if (!controller.signal.aborted) setStatsLoading(false); });
    return () => controller.abort();
  }, [revision]);
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setListError(null);
    getAccountPage(keyword, status, offset, controller.signal).then(value => {
      if (controller.signal.aborted) return;
      if (offset && offset >= value.total) { setOffset(Math.max(0, Math.ceil(value.total / 20) - 1) * 20); return; }
      setPage(value);
      setSelected(current => value.items.find(item => item.id === (preferred.current ?? current?.id)) ?? (preferred.current && current?.id === preferred.current ? current : value.items[0] ?? null));
      preferred.current = null;
    }).catch(error => { if (!controller.signal.aborted) { setPage(null); setSelected(null); setListError(failure(error)); } }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [keyword, status, offset, revision]);
  useEffect(() => { setUsageOffset(0); }, [selected?.id]);
  useEffect(() => {
    const controller = new AbortController(); setUsageError(null);
    const scope = `${selected?.id ?? ""}:${usageOffset}`;
    if (usageScope.current !== scope) setUsage(null);
    usageScope.current = scope;
    if (!selected) { setUsageLoading(false); return () => controller.abort(); }
    setUsageLoading(true);
    getAccountUsage(selected.id, usageOffset, controller.signal).then(value => { if (!controller.signal.aborted) setUsage(value); }).catch(error => { if (!controller.signal.aborted) setUsageError(failure(error)); }).finally(() => { if (!controller.signal.aborted) setUsageLoading(false); });
    return () => controller.abort();
  }, [selected?.id, usageOffset, revision]);
  function refresh() { setNotice(null); setRevision(value => value + 1); }
  function saved(profile: AccountProfile, message: string) {
    setEditor(null); preferred.current = profile.id; setSelected(profile); setNotice(message); setNoticeSequence(value => value + 1);
    // Apply the returned object immediately; retain existing rows and filters
    // during edit revalidation instead of blanking the catalogue.
    setPage(current => current ? { ...current, items: current.items.map(item => item.id === profile.id ? profile : item) } : current);
    if (editor && (!editor.source || editor.copy)) { setQuery(""); setKeyword(""); setStatus(""); setOffset(0); setUsageOffset(0); }
    setRevision(value => value + 1);
  }
  function deleted(id: string) {
    setDeleting(null); preferred.current = null;
    const remaining = page?.items.filter(item => item.id !== id) ?? [];
    setPage(current => current ? { ...current, total: Math.max(0, current.total - 1), items: remaining } : current);
    setSelected(current => current?.id === id ? remaining[0] ?? null : current);
    setNotice("账户已永久删除。"); setNoticeSequence(value => value + 1);
    setRevision(value => value + 1);
  }
  const tabs = [["overview", "概览"], ["fees", "费用规则"], ["metadata", "账户元数据"]];
  return <section className="qfac-page" aria-labelledby="qfac-title">
    <header className="qfac-page-head"><div><div className="qfac-eyebrow">BACKTEST ACCOUNTS</div><h1 id="qfac-title">回测账户</h1><p>管理回测使用的账户、费用规则与账户元数据。</p></div><div className="qfac-actions"><button className="qfac-button" disabled={loading || statsLoading} onClick={refresh}><RefreshCw className={loading || statsLoading ? "spin" : ""} />刷新</button><button className="qfac-button qfac-primary" onClick={() => setEditor({ copy: false })}><Plus />新建账户</button></div></header>
    {notice && <div className="qfac-notice qfac-toast" role="status"><span>{notice}</span><button type="button" className="qfac-icon" aria-label="关闭成功提示" onClick={() => setNotice(null)}><X aria-hidden="true" /></button></div>}
    {statsError && <div className="qfac-error" role="alert">统计加载失败：{statsError}</div>}
    <section className="qfac-metrics" aria-label="账户全量统计" aria-busy={statsLoading}>
      <div><span>账户总数</span><strong>{overview?.total_accounts ?? (statsLoading ? "…" : "—")}</strong><small>{overview ? `${overview.active_accounts} 个可选择，${overview.inactive_accounts} 个停用，${overview.retired_accounts} 个已退役` : statsError ? "统计暂不可用" : "正在加载"}</small></div>
      <div><span>费用规则</span><strong>{overview?.total_fee_rules ?? (statsLoading ? "…" : "—")}</strong><small>{overview ? `分布在 ${overview.total_accounts} 个账户的当前配置中` : "各账户当前配置的规则总数"}</small></div>
      <div><span>关联策略版本</span><strong>{overview?.related_strategy_versions ?? (statsLoading ? "…" : "—")}</strong><small>{overview ? `${overview.used_accounts} 个账户有正式回测记录` : "当前用户可见的正式回测引用"}</small></div>
    </section>
    <div className="qfac-workspace">
      <section className="qfac-sheet qfac-catalog" aria-label="账户目录"><header className="qfac-sheet-head"><h2>账户</h2><span>{page ? `${page.total} 项` : loading ? "加载中" : "加载失败"}</span></header>
        <div className="qfac-filter-row"><label className="qfac-search"><Search /><input aria-label="搜索名称或费用方案标识" placeholder="搜索名称或费用方案标识" value={query} onChange={e => setQuery(e.target.value)} /></label><select aria-label="账户状态" value={status} onChange={e => { setStatus(e.target.value as AccountProfileStatus | ""); setOffset(0); }}><option value="">全部状态</option>{Object.entries(STATUS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></div>
        <div className="qfac-catalog-scroll" aria-busy={loading}>{loading && !page ? <div className="qfac-empty" role="status"><LoaderCircle className="spin" />正在加载账户…</div> : listError ? <div className="qfac-empty" role="alert"><CircleAlert /><p>{listError}</p><button className="qfac-button" onClick={refresh}>重试</button></div> : !page?.items.length ? <div className="qfac-empty"><strong>没有匹配的账户</strong><p>调整筛选条件，或创建一个新账户。</p></div> : page.items.map(profile => <button key={profile.id} disabled={loading} className={`qfac-account-row ${selected?.id === profile.id ? "qfac-selected" : ""}`} aria-pressed={selected?.id === profile.id} onClick={() => { setSelected(profile); setUsageOffset(0); }}><div><strong>{profile.name}</strong><small>{profile.fee_schedule.key} · {profile.fee_schedule.fee_rules.length} 条费用规则</small></div><div><Badge status={profile.status} /><time dateTime={profile.updated_at}>{formatTime(profile.updated_at).split(" ")[0]}</time></div></button>)}</div>
        <Pager total={page?.total ?? 0} limit={20} offset={offset} busy={loading} onChange={setOffset} label="账户列表" />
      </section>
      <section className="qfac-sheet qfac-detail" aria-label="账户详情">
        {selected ? <><header className="qfac-detail-head"><div><div className="qfac-eyebrow">ACCOUNT OBJECT</div><div className="qfac-object-title"><h2>{selected.name}</h2><code>{selected.fee_schedule.key}</code><Badge status={selected.status} /></div></div><div className="qfac-actions"><button className="qfac-button" disabled={loading} onClick={() => setEditor({ source: selected, copy: true })}><Copy />复制账户</button><button className="qfac-button" disabled={loading} onClick={() => setEditor({ source: selected, copy: false })}>编辑</button><button className="qfac-button qfac-delete-action" disabled={loading} onClick={() => setDeleting(selected)}>删除</button></div></header>
        <div className="qfac-tabs" role="tablist" aria-label="账户详情页签">{tabs.map(([key, label], index) => <button key={key} role="tab" id={`qfac-tab-${key}`} aria-controls="qfac-panel" aria-selected={tab === key} tabIndex={tab === key ? 0 : -1} onClick={() => setTab(key)} onKeyDown={e => { let next = index; if (e.key === "ArrowRight") next = (index + 1) % tabs.length; else if (e.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length; else if (e.key === "Home") next = 0; else if (e.key === "End") next = tabs.length - 1; else return; e.preventDefault(); setTab(tabs[next][0]); document.getElementById(`qfac-tab-${tabs[next][0]}`)?.focus(); }}>{label}</button>)}</div>
        <div className="qfac-detail-body" id="qfac-panel" role="tabpanel" aria-labelledby={`qfac-tab-${tab}`} tabIndex={0}>
          {tab === "overview" && <div className="qfac-overview-grid"><Card title="账户资料"><Properties values={[["账户名称", selected.name], ["状态", <Badge status={selected.status} />], ["费用方案标识", selected.fee_schedule.key], ["账户 / 费用版本", `v${selected.version} / v${selected.fee_schedule_version}`], ...Object.entries(META).map(([key, label]): [string, ReactNode] => [label, typeof selected.metadata[key] === "string" ? String(selected.metadata[key]) || "（空字符串）" : selected.metadata[key] != null ? JSON.stringify(selected.metadata[key]) : "—"]), ["创建时间", formatTime(selected.created_at)], ["最近更新", formatTime(selected.updated_at)], ["账户 UUID", selected.id]]} /></Card>
          <div className="qfac-overview-side"><Card title="回测使用记录" action={<span>{usage ? `${usage.related_strategy_versions} 个策略版本` : "—"}</span>}>
            {usageLoading && !usage ? <p className="qfac-card-message" role="status">正在加载使用记录…</p> : usageError ? <div className="qfac-card-message qfac-error" role="alert">{usageError}<button className="qfac-button" onClick={refresh}>重试</button></div> : !usage?.items.length ? <p className="qfac-card-message">暂无正式回测使用记录</p> : <div className="qfac-usage-list">{usage.items.map(item => <article key={item.latest_run_id}><strong>{item.strategy_name ?? "未知策略"}{item.revision_number != null ? ` · v${item.revision_number}` : ""}</strong><p>账户版本 {item.account_profile_version ?? "未知"} · {item.run_count} 次回测</p>{!item.strategy_name && <code>{item.strategy_revision_id ?? "未记录策略版本"}</code>}<small>最近提交 {formatTime(item.latest_run_at)} · {RUN_STATUS[item.latest_run_status] ?? "未知状态"}</small></article>)}</div>}
            {usage && usage.total > 5 && <Pager total={usage.total} offset={usageOffset} limit={5} busy={usageLoading} onChange={setUsageOffset} label="使用记录" />}<p className="qfac-note">历史正式回测引用，包含失败或取消的提交；不代表当前绑定或正在使用。</p>
          </Card><Card title="费用摘要" action={<button className="qfac-button" onClick={() => setTab("fees")}>查看规则</button>}><Properties values={[["规则数", `${selected.fee_schedule.fee_rules.length} 条`], ["费用版本", `v${selected.fee_schedule_version}`], ...selected.fee_schedule.fee_rules.map((rule, index): [string, ReactNode] => [`${CATEGORIES[rule.category] ?? rule.category} ${index + 1}`, `费率 ${rule.rate} · 最低 ${rule.minimum}`])]} /></Card></div></div>}
          {tab === "fees" && <div className="qfac-fee-list">{selected.fee_schedule.fee_rules.map((rule, index) => <FeeCard key={`${index}-${rule.key}`} rule={rule} />)}{Object.keys(selected.fee_schedule.metadata).length > 0 && <Card title="费用方案元数据"><Properties values={Object.entries(selected.fee_schedule.metadata)} /></Card>}</div>}
          {tab === "metadata" && <Card title="账户元数据">{Object.keys(selected.metadata).length ? <table className="qfac-metadata-table"><thead><tr><th>字段</th><th>值</th><th>说明</th></tr></thead><tbody>{Object.entries(selected.metadata).map(([key, value]) => <tr key={key}><th scope="row">{key}</th><td>{typeof value === "string" ? value || "（空字符串）" : JSON.stringify(value)}</td><td>{META[key] ?? "自定义字段"}</td></tr>)}</tbody></table> : <p className="qfac-card-message">暂无账户元数据</p>}</Card>}
        </div></> : <div className="qfac-empty">{loading && !page ? "正在加载账户详情…" : "选择账户查看配置"}</div>}
      </section>
    </div>
    {deleting && <AccountDeleteDialog account={deleting} onClose={() => setDeleting(null)} onDeleted={deleted} onRetired={profile => { setDeleting(null); saved(profile, "账户已退役，历史配置和回测记录已保留。"); }} />}
    {editor && <AccountWizard source={editor.source} copy={editor.copy} onClose={() => setEditor(null)} onSaved={saved} />}
  </section>;
}
