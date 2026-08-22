import { CircleAlert, CircleCheck, LoaderCircle, Plus, RefreshCw, Search, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { useAuth } from "../auth/AuthContext";
import {
  AccountProfile,
  AccountProfileApiError,
  AccountProfilePayload,
  AccountProfileStatus,
  createAccountProfile,
  listAccountProfiles,
  updateAccountProfile,
} from "../api/accountProfiles";

const EMPTY_RULE = {
  key: "commission",
  category: "commission",
  side: null,
  rate: "0.0003",
  minimum: "5",
  fixed_amount: "0",
  rounding_level: "fee_item" as const,
  rounding_scope: "commission",
  rounding_mode: "half_up" as const,
  rounding_precision: "0.01",
  applicability: {},
};

function defaultPayload(): AccountProfilePayload {
  return {
    name: "",
    status: "active",
    fee_schedule: { key: "", fee_rules: [EMPTY_RULE], metadata: {} },
    metadata: {},
  };
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "short" }).format(new Date(value));
}

function parseObject(value: string, label: string): Record<string, string> {
  const parsed: unknown = JSON.parse(value || "{}");
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error(`${label}必须是 JSON 对象。`);
  return parsed as Record<string, string>;
}

export function AccountProfilesPage() {
  const { logout } = useAuth();
  const [profiles, setProfiles] = useState<AccountProfile[]>([]);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState<AccountProfileStatus | "">("active");
  const [selected, setSelected] = useState<AccountProfile | null>(null);
  const [draft, setDraft] = useState<AccountProfilePayload>(defaultPayload());
  const [rulesText, setRulesText] = useState(JSON.stringify([EMPTY_RULE], null, 2));
  const [metadataText, setMetadataText] = useState("{}");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async (background = false) => {
    if (background) setRefreshing(true); else setLoading(true);
    setError(null);
    try {
      const next = await listAccountProfiles(query, status);
      setProfiles(next);
      if (selected && !next.some((item) => item.id === selected.id)) setSelected(null);
    } catch (caught) {
      if (caught instanceof AccountProfileApiError && caught.status === 401) {
        logout();
        return;
      }
      setError(caught instanceof Error ? caught.message : "账户档案加载失败。");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [logout, query, selected, status]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 180);
    return () => window.clearTimeout(timer);
  }, [load]);

  const activeCount = useMemo(() => profiles.filter((item) => item.status === "active").length, [profiles]);

  function openCreate() {
    const next = defaultPayload();
    setSelected(null);
    setDraft(next);
    setRulesText(JSON.stringify(next.fee_schedule.fee_rules, null, 2));
    setMetadataText("{}");
    setEditing(true);
    setError(null);
  }

  function openEdit(profile: AccountProfile) {
    const next: AccountProfilePayload = {
      name: profile.name,
      status: profile.status,
      fee_schedule: profile.fee_schedule,
      metadata: Object.fromEntries(Object.entries(profile.metadata).filter(([, value]) => typeof value === "string")) as Record<string, string>,
    };
    setSelected(profile);
    setDraft(next);
    setRulesText(JSON.stringify(next.fee_schedule.fee_rules, null, 2));
    setMetadataText(JSON.stringify(next.metadata, null, 2));
    setEditing(true);
    setError(null);
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const feeRules = JSON.parse(rulesText) as AccountProfilePayload["fee_schedule"]["fee_rules"];
      const metadata = parseObject(metadataText, "账户元数据");
      const payload: AccountProfilePayload = {
        ...draft,
        name: draft.name.trim(),
        fee_schedule: { ...draft.fee_schedule, key: draft.fee_schedule.key.trim(), fee_rules: feeRules, metadata: {} },
        metadata,
      };
      const saved = selected ? await updateAccountProfile(selected.id, payload) : await createAccountProfile(payload);
      setNotice(selected ? "账户档案已更新。" : "账户档案已创建。" );
      setEditing(false);
      setSelected(saved);
      await load(true);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "账户档案保存失败。");
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="account-profiles-page" aria-labelledby="account-profiles-title">
      <div className="page-heading">
        <div><span className="workbench-eyebrow">BACKTEST ACCOUNTS</span><h2 id="account-profiles-title">回测账户档案</h2><p>账户必须显式选择；费用配置会由后续回测运行冻结。</p></div>
        <div className="account-profiles__actions"><button className="toolbar-button" type="button" disabled={refreshing} onClick={() => void load(true)}><RefreshCw className={refreshing ? "spin" : ""} aria-hidden="true" />刷新</button><button className="task-create-button" type="button" onClick={openCreate}><Plus aria-hidden="true" />新建账户</button></div>
      </div>
      {error && <div className="page-error" role="alert"><CircleAlert aria-hidden="true" />{error}</div>}
      {notice && <div className="strategy-message strategy-message--success" role="status"><CircleCheck aria-hidden="true" />{notice}</div>}
      <div className="account-profiles__stats"><div><span>当前结果</span><strong>{profiles.length}</strong><small>支持按账户名称筛选</small></div><div><span>可选择账户</span><strong>{activeCount}</strong><small>仅 active 出现在运行选择器</small></div></div>
      <div className="account-profiles__toolbar"><label className="search-field"><Search aria-hidden="true" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="按账户名称筛选…" aria-label="按账户名称筛选" /></label><select value={status} onChange={(event) => setStatus(event.target.value as AccountProfileStatus | "")} aria-label="账户状态"><option value="active">可选择</option><option value="">全部状态</option><option value="inactive">停用</option><option value="retired">已退役</option></select><label className="account-profile-selector"><span>显式选择账户</span><select value={selected?.id ?? ""} onChange={(event) => { const next = profiles.find((item) => item.id === event.target.value); if (next) setSelected(next); }} aria-label="显式选择账户"><option value="">请选择账户</option>{profiles.filter((item) => item.status === "active").map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label></div>
      <div className="account-profiles__layout">
        <section className="account-profiles__list" aria-label="账户档案列表">
          {loading ? <div className="account-profiles__empty"><LoaderCircle className="spin" aria-hidden="true" />正在加载账户…</div> : profiles.length === 0 ? <div className="account-profiles__empty"><strong>没有匹配的账户</strong><span>请调整名称筛选，或创建一个新账户。</span></div> : profiles.map((profile) => <button type="button" key={profile.id} className={`account-profile-row${selected?.id === profile.id ? " account-profile-row--selected" : ""}`} onClick={() => openEdit(profile)}><span className={`account-profile-row__dot account-profile-row__dot--${profile.status}`} aria-hidden="true" /><span><strong>{profile.name}</strong><small>{profile.fee_schedule.key}</small></span><em>{profile.status === "active" ? "可选择" : profile.status === "inactive" ? "停用" : "已退役"}<br /><time>{formatTime(profile.updated_at)}</time></em></button>)}
        </section>
        <section className="account-profiles__detail" aria-label="账户档案说明"><span className="workbench-eyebrow">EXPLICIT SELECTION</span><h3>账户名称是选择入口</h3><p>回测创建不会读取任何默认账户或默认费用方案。后续运行页面应通过账户 ID 提交选择，并使用这里的账户名称进行搜索和展示。</p><dl><div><dt>费用方案</dt><dd>{selected?.fee_schedule.key ?? "尚未选择账户"}</dd></div><div><dt>最近更新</dt><dd>{selected ? formatTime(selected.updated_at) : "—"}</dd></div></dl></section>
      </div>
      {editing && <div className="prototype-modal account-profile-modal" role="presentation" onMouseDown={() => setEditing(false)}><form className="prototype-modal__dialog" onSubmit={(event) => void save(event)} onMouseDown={(event) => event.stopPropagation()}><div className="prototype-modal__heading"><div><span>{selected ? "EDIT ACCOUNT" : "NEW ACCOUNT"}</span><h3>{selected ? "编辑账户档案" : "新建账户档案"}</h3></div><button className="icon-button" type="button" onClick={() => setEditing(false)} aria-label="关闭账户编辑"><X aria-hidden="true" /></button></div><label><span>账户名称</span><input autoFocus required maxLength={100} value={draft.name} onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))} placeholder="例如：ETF 研究账户" /></label><label><span>状态</span><select value={draft.status} onChange={(event) => setDraft((current) => ({ ...current, status: event.target.value as AccountProfileStatus }))}><option value="active">可选择</option><option value="inactive">停用</option><option value="retired">已退役</option></select></label><label><span>费用方案 Key</span><input required maxLength={100} value={draft.fee_schedule.key} onChange={(event) => setDraft((current) => ({ ...current, fee_schedule: { ...current.fee_schedule, key: event.target.value } }))} placeholder="例如：etf-cny" /></label><label><span>费用规则（JSON 数组）</span><textarea required value={rulesText} onChange={(event) => setRulesText(event.target.value)} spellCheck={false} /></label><label><span>账户元数据（JSON 对象）</span><textarea required value={metadataText} onChange={(event) => setMetadataText(event.target.value)} spellCheck={false} /></label><div className="prototype-modal__actions"><button className="secondary-button" type="button" onClick={() => setEditing(false)}>取消</button><button className="task-create-button" type="submit" disabled={saving || !draft.name.trim() || !draft.fee_schedule.key.trim()}>{saving && <LoaderCircle className="spin" aria-hidden="true" />}{saving ? "保存中" : "保存账户"}</button></div></form></div>}
    </section>
  );
}
