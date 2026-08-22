import { CircleAlert, CircleCheck, LoaderCircle, Plus, RefreshCw, Search, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";

import { useAuth } from "../auth/AuthContext";
import {
  AccountProfile,
  AccountProfileApiError,
  AccountProfilePayload,
  AccountProfileStatus,
  FeeRule,
  createAccountProfile,
  listAccountProfiles,
  updateAccountProfile,
} from "../api/accountProfiles";

type ApplicabilityEntry = { key: string; value: string };
type MetadataEntry = { key: string; value: string };

const FEE_ITEM_OPTIONS = [
  { value: "commission", label: "佣金（Commission）", description: "按成交金额比例收取的交易佣金。" },
  { value: "stamp_tax", label: "印花税（Stamp Tax）", description: "按适用方向收取的证券交易印花税。" },
  { value: "transfer_fee", label: "过户费（Transfer Fee）", description: "证券登记或过户产生的费用。" },
  { value: "handling_fee", label: "经手费（Handling Fee）", description: "交易所或通道收取的经手费用。" },
  { value: "regulatory_fee", label: "监管费（Regulatory Fee）", description: "监管机构或市场规则产生的费用。" },
  { value: "other", label: "其他费用（Other）", description: "不属于上述类别的费用。" },
] as const;

const ACCOUNT_METADATA_FIELDS = [
  { key: "currency", label: "结算币种（Currency）", placeholder: "例如：CNY", help: "账户现金、费用和净值使用的结算币种。" },
  { key: "account_type", label: "账户类型（Account Type）", placeholder: "例如：cash", help: "例如 cash（现金）或 margin（保证金）。" },
  { key: "broker", label: "交易通道（Broker / Venue）", placeholder: "例如：模拟交易通道", help: "填写券商、交易所或模拟交易通道名称。" },
  { key: "owner", label: "账户归属（Owner）", placeholder: "例如：研究团队", help: "用于内部识别账户负责人或使用团队。" },
] as const;

function feeItemOptions(currentValue: string): ReactNode {
  const isKnown = FEE_ITEM_OPTIONS.some((option) => option.value === currentValue);
  return (
    <>
      {!isKnown && currentValue && <option value={currentValue}>兼容值（{currentValue}）</option>}
      {FEE_ITEM_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
    </>
  );
}

function feeItemHelp(currentValue: string): string {
  return FEE_ITEM_OPTIONS.find((option) => option.value === currentValue)?.description ?? `历史兼容费用项：${currentValue}`;
}

function emptyFeeRule(feeItem = "commission"): FeeRule {
  return {
    key: feeItem,
    category: feeItem,
    side: null,
    rate: "0.0003",
    minimum: "5",
    fixed_amount: "0",
    rounding_level: "fee_item",
    rounding_scope: feeItem,
    rounding_mode: "half_up",
    rounding_precision: "0.01",
    applicability: {},
  };
}

function normalizeFeeRule(rule: FeeRule): FeeRule {
  // API decimals are normally returned as strings; normalize older numeric
  // responses here so the controlled inputs always receive text values.
  return {
    ...emptyFeeRule(),
    ...rule,
    side: rule.side ?? null,
    rate: String(rule.rate ?? "0"),
    minimum: String(rule.minimum ?? "0"),
    fixed_amount: String(rule.fixed_amount ?? "0"),
    rounding_scope: rule.rounding_scope ?? null,
    rounding_precision: rule.rounding_precision == null ? null : String(rule.rounding_precision),
    applicability: { ...(rule.applicability ?? {}) },
  };
}

function applicabilityEntries(rule: FeeRule): ApplicabilityEntry[] {
  return Object.entries(rule.applicability ?? {}).map(([key, value]) => ({ key, value }));
}

function metadataEntries(metadata: Record<string, unknown>): { values: Record<string, string>; extras: MetadataEntry[] } {
  const stringEntries = Object.entries(metadata).filter(([, value]) => typeof value === "string") as [string, string][];
  const knownKeys: Set<string> = new Set(ACCOUNT_METADATA_FIELDS.map((field) => field.key));
  return {
    values: Object.fromEntries(stringEntries.filter(([key]) => knownKeys.has(key))),
    extras: stringEntries.filter(([key]) => !knownKeys.has(key)).map(([key, value]) => ({ key, value })),
  };
}

function defaultPayload(): AccountProfilePayload {
  return {
    name: "",
    status: "active",
    fee_schedule: { key: "", fee_rules: [emptyFeeRule()], metadata: {} },
    metadata: {},
  };
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "short" }).format(new Date(value));
}

export function AccountProfilesPage() {
  const { logout } = useAuth();
  const [profiles, setProfiles] = useState<AccountProfile[]>([]);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState<AccountProfileStatus | "">("active");
  const [selected, setSelected] = useState<AccountProfile | null>(null);
  const [draft, setDraft] = useState<AccountProfilePayload>(defaultPayload());
  const [applicabilityRows, setApplicabilityRows] = useState<ApplicabilityEntry[][]>([[]]);
  const [metadataValues, setMetadataValues] = useState<Record<string, string>>({});
  const [metadataRows, setMetadataRows] = useState<MetadataEntry[]>([]);
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
    setApplicabilityRows([[]]);
    setMetadataValues({});
    setMetadataRows([]);
    setEditing(true);
    setError(null);
  }

  function openEdit(profile: AccountProfile) {
    const feeRules = profile.fee_schedule.fee_rules.map(normalizeFeeRule);
    const profileMetadata = metadataEntries(profile.metadata);
    const next: AccountProfilePayload = {
      name: profile.name,
      status: profile.status,
      fee_schedule: { ...profile.fee_schedule, fee_rules: feeRules },
      metadata: profileMetadata.values,
    };
    setSelected(profile);
    setDraft(next);
    setApplicabilityRows(feeRules.map(applicabilityEntries));
    setMetadataValues(profileMetadata.values);
    setMetadataRows(profileMetadata.extras);
    setEditing(true);
    setError(null);
  }

  function updateFeeRule(index: number, patch: Partial<FeeRule>) {
    setDraft((current) => ({
      ...current,
      fee_schedule: {
        ...current.fee_schedule,
        fee_rules: current.fee_schedule.fee_rules.map((rule, ruleIndex) => (
          ruleIndex === index ? { ...rule, ...patch } : rule
        )),
      },
    }));
  }

  function addFeeRule() {
    setDraft((current) => {
      const usedKeys = new Set(current.fee_schedule.fee_rules.map((rule) => rule.key));
      const option = FEE_ITEM_OPTIONS.find((item) => !usedKeys.has(item.value)) ?? FEE_ITEM_OPTIONS[FEE_ITEM_OPTIONS.length - 1];
      return {
        ...current,
        fee_schedule: {
          ...current.fee_schedule,
          fee_rules: [...current.fee_schedule.fee_rules, emptyFeeRule(option.value)],
        },
      };
    });
    setApplicabilityRows((current) => [...current, []]);
  }

  function removeFeeRule(index: number) {
    if (draft.fee_schedule.fee_rules.length <= 1) return;
    setDraft((current) => ({
      ...current,
      fee_schedule: {
        ...current.fee_schedule,
        fee_rules: current.fee_schedule.fee_rules.filter((_, ruleIndex) => ruleIndex !== index),
      },
    }));
    setApplicabilityRows((current) => current.filter((_, ruleIndex) => ruleIndex !== index));
  }

  function updateApplicability(index: number, entryIndex: number, patch: Partial<ApplicabilityEntry>) {
    setApplicabilityRows((current) => current.map((entries, ruleIndex) => (
      ruleIndex === index
        ? entries.map((entry, currentEntryIndex) => currentEntryIndex === entryIndex ? { ...entry, ...patch } : entry)
        : entries
    )));
  }

  function addApplicability(index: number) {
    setApplicabilityRows((current) => current.map((entries, ruleIndex) => (
      ruleIndex === index ? [...entries, { key: "", value: "" }] : entries
    )));
  }

  function removeApplicability(index: number, entryIndex: number) {
    setApplicabilityRows((current) => current.map((entries, ruleIndex) => (
      ruleIndex === index ? entries.filter((_, currentEntryIndex) => currentEntryIndex !== entryIndex) : entries
    )));
  }

  function addMetadataRow() {
    setMetadataRows((current) => [...current, { key: "", value: "" }]);
  }

  function updateMetadataRow(index: number, patch: Partial<MetadataEntry>) {
    setMetadataRows((current) => current.map((entry, entryIndex) => (
      entryIndex === index ? { ...entry, ...patch } : entry
    )));
  }

  function removeMetadataRow(index: number) {
    setMetadataRows((current) => current.filter((_, entryIndex) => entryIndex !== index));
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const feeRules = draft.fee_schedule.fee_rules.map((rule, index) => {
        const applicability: Record<string, string> = {};
        for (const entry of applicabilityRows[index] ?? []) {
          const key = entry.key.trim();
          const value = entry.value.trim();
          if (!key && !value) continue;
          if (!key || !value) throw new Error(`费用规则 ${index + 1} 的适用条件必须同时填写名称和值。`);
          if (applicability[key]) throw new Error(`费用规则 ${index + 1} 的适用条件名称“${key}”重复。`);
          applicability[key] = value;
        }
        return {
          ...normalizeFeeRule(rule),
          key: rule.key,
          category: rule.category,
          side: rule.side || null,
          rounding_scope: rule.rounding_scope?.trim() || null,
          applicability,
        };
      });
      const metadata: Record<string, string> = {};
      for (const field of ACCOUNT_METADATA_FIELDS) {
        const value = (metadataValues[field.key] ?? "").trim();
        if (value) metadata[field.key] = value;
      }
      const knownMetadataKeys: Set<string> = new Set(ACCOUNT_METADATA_FIELDS.map((field) => field.key));
      for (const entry of metadataRows) {
        const key = entry.key.trim();
        const value = entry.value.trim();
        if (!key && !value) continue;
        if (!key || !value) throw new Error("自定义账户元数据必须同时填写名称和值。");
        if (knownMetadataKeys.has(key)) throw new Error(`自定义账户元数据名称“${key}”已由上方字段提供。`);
        if (metadata[key]) throw new Error(`账户元数据名称“${key}”重复。`);
        metadata[key] = value;
      }
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
          {loading ? <div className="account-profiles__empty"><LoaderCircle className="spin" aria-hidden="true" />正在加载账户…</div> : profiles.length === 0 ? <div className="account-profiles__empty"><strong>没有匹配的账户</strong><span>请调整名称筛选，或创建一个新账户。</span></div> : profiles.map((profile) => <button type="button" key={profile.id} className={`account-profile-row${selected?.id === profile.id ? " account-profile-row--selected" : ""}`} onClick={() => openEdit(profile)}><span className={`account-profile-row__dot account-profile-row__dot--${profile.status}`} aria-hidden="true" /><span><strong>{profile.name}</strong><small>{profile.fee_schedule.key}</small></span><em>{profile.status === "active" ? "可选择" : profile.status === "inactive" ? "停用" : "已退役"}<br /><time>{formatTime(profile.updated_at)}</time></em></button>) }
        </section>
        <section className="account-profiles__detail" aria-label="账户档案说明"><span className="workbench-eyebrow">EXPLICIT SELECTION</span><h3>账户名称是选择入口</h3><p>回测创建不会读取任何默认账户或默认费用方案。后续运行页面应通过账户 ID 提交选择，并使用这里的账户名称进行搜索和展示。</p><dl><div><dt>费用方案</dt><dd>{selected?.fee_schedule.key ?? "尚未选择账户"}</dd></div><div><dt>最近更新</dt><dd>{selected ? formatTime(selected.updated_at) : "—"}</dd></div></dl></section>
      </div>
      {editing && (
        <div className="prototype-modal account-profile-modal" role="presentation" onMouseDown={() => setEditing(false)}>
          <form className="prototype-modal__dialog" onSubmit={(event) => void save(event)} onMouseDown={(event) => event.stopPropagation()}>
            <div className="prototype-modal__heading"><div><span>{selected ? "EDIT ACCOUNT" : "NEW ACCOUNT"}</span><h3>{selected ? "编辑账户档案" : "新建账户档案"}</h3></div><button className="icon-button" type="button" onClick={() => setEditing(false)} aria-label="关闭账户编辑"><X aria-hidden="true" /></button></div>
            <p className="account-profile-modal__intro">费用规则已拆成可直接填写的字段。费率填写小数（例如 0.03% 填写为 0.0003），金额字段使用账户结算币种；每条规则都必须配置完整的取整方式。</p>
            <label><span>账户名称</span><input autoFocus required maxLength={100} value={draft.name} onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))} placeholder="例如：ETF 研究账户" /></label>
            <label><span>状态</span><select value={draft.status} onChange={(event) => setDraft((current) => ({ ...current, status: event.target.value as AccountProfileStatus }))}><option value="active">可选择</option><option value="inactive">停用</option><option value="retired">已退役</option></select></label>
            <label><span>费用方案 Key <small className="field-help">费用方案的稳定标识，只允许在同一账户档案内使用一个名称。</small></span><input required maxLength={100} value={draft.fee_schedule.key} onChange={(event) => setDraft((current) => ({ ...current, fee_schedule: { ...current.fee_schedule, key: event.target.value } }))} placeholder="例如：etf-cny" /></label>
            <section className="fee-rules-editor" aria-labelledby="fee-rules-title">
              <div className="fee-rules-editor__heading"><div><h4 id="fee-rules-title">费用规则</h4><p>一条规则代表一种费用，例如买卖佣金、印花税或过户费。</p></div><button className="secondary-button fee-rules-editor__add" type="button" onClick={addFeeRule}><Plus aria-hidden="true" />新增费用项</button></div>
              {draft.fee_schedule.fee_rules.map((rule, index) => (
                <article className="fee-rule-editor" key={`${index}-${rule.key}`}>
                  <div className="fee-rule-editor__heading"><div><span>FEE RULE {String(index + 1).padStart(2, "0")}</span><h5>费用项 {index + 1}</h5></div>{draft.fee_schedule.fee_rules.length > 1 && <button className="icon-button fee-rule-editor__remove" type="button" onClick={() => removeFeeRule(index)} aria-label={`删除费用项 ${index + 1}`}><Trash2 aria-hidden="true" /></button>}</div>
                  <div className="fee-rule-editor__grid">
                    <label><span>规则 Key <small className="field-help">保存为英文机器标识；{feeItemHelp(rule.key)}</small></span><select required value={rule.key} onChange={(event) => { const nextKey = event.target.value; updateFeeRule(index, { key: nextKey, rounding_scope: rule.rounding_scope === rule.key ? nextKey : rule.rounding_scope }); }}>{feeItemOptions(rule.key)}</select></label>
                    <label><span>费用项 <small className="field-help">用于结果明细分类；{feeItemHelp(rule.category)}</small></span><select required value={rule.category} onChange={(event) => updateFeeRule(index, { category: event.target.value })}>{feeItemOptions(rule.category)}</select></label>
                    <label><span>适用方向 <small className="field-help">不限制方向时选择“买入和卖出”。</small></span><select value={rule.side ?? ""} onChange={(event) => updateFeeRule(index, { side: event.target.value || null })}><option value="">买入和卖出</option><option value="buy">仅买入</option><option value="sell">仅卖出</option></select></label>
                    <label><span>费率 <small className="field-help">按成交金额比例填写；0.03% = 0.0003。</small></span><input required type="number" min="0" step="any" value={rule.rate} onChange={(event) => updateFeeRule(index, { rate: event.target.value })} placeholder="0.0003" /></label>
                    <label><span>最低费用 <small className="field-help">单项最低收费；填写 0 表示没有最低费用。</small></span><input required type="number" min="0" step="0.01" value={rule.minimum} onChange={(event) => updateFeeRule(index, { minimum: event.target.value })} placeholder="5" /></label>
                    <label><span>固定费用 <small className="field-help">每笔额外固定收取的金额；没有则填写 0。</small></span><input required type="number" min="0" step="0.01" value={rule.fixed_amount} onChange={(event) => updateFeeRule(index, { fixed_amount: event.target.value })} placeholder="0" /></label>
                    <label><span>取整层级 <small className="field-help">费用项分别取整、成交汇总取整或订单汇总取整。</small></span><select required value={rule.rounding_level ?? ""} onChange={(event) => updateFeeRule(index, { rounding_level: (event.target.value || null) as FeeRule["rounding_level"] })}><option value="fee_item">费用项</option><option value="fill">成交</option><option value="order">订单</option></select></label>
                    <label><span>取整范围 <small className="field-help">填写这条规则覆盖的口径，如 commission。</small></span><input required maxLength={100} value={rule.rounding_scope ?? ""} onChange={(event) => updateFeeRule(index, { rounding_scope: event.target.value })} placeholder="commission" /></label>
                    <label><span>取整方式 <small className="field-help">向上、向下或四舍五入。</small></span><select required value={rule.rounding_mode ?? ""} onChange={(event) => updateFeeRule(index, { rounding_mode: (event.target.value || null) as FeeRule["rounding_mode"] })}><option value="half_up">四舍五入</option><option value="up">向上取整</option><option value="down">向下取整</option></select></label>
                    <label><span>取整精度 <small className="field-help">最小货币单位，例如人民币填写 0.01。</small></span><input required type="number" min="0.00000001" step="any" value={rule.rounding_precision ?? ""} onChange={(event) => updateFeeRule(index, { rounding_precision: event.target.value })} placeholder="0.01" /></label>
                  </div>
                  <div className="fee-rule-editor__conditions"><div className="fee-rule-editor__conditions-heading"><div><strong>适用条件</strong><small>可选；用名称和值限定资产类别、市场等范围，例如 asset_class = etf。</small></div><button className="secondary-button" type="button" onClick={() => addApplicability(index)}><Plus aria-hidden="true" />新增条件</button></div>{(applicabilityRows[index] ?? []).length === 0 ? <p className="fee-rule-editor__conditions-empty">未设置适用条件，这条费用规则将适用于所有资产。</p> : (applicabilityRows[index] ?? []).map((entry, entryIndex) => <div className="fee-rule-editor__condition" key={`${index}-${entryIndex}`}><input aria-label={`费用项 ${index + 1} 适用条件名称 ${entryIndex + 1}`} value={entry.key} onChange={(event) => updateApplicability(index, entryIndex, { key: event.target.value })} placeholder="条件名称，如 asset_class" /><span>=</span><input aria-label={`费用项 ${index + 1} 适用条件值 ${entryIndex + 1}`} value={entry.value} onChange={(event) => updateApplicability(index, entryIndex, { value: event.target.value })} placeholder="条件值，如 etf" /><button className="icon-button" type="button" onClick={() => removeApplicability(index, entryIndex)} aria-label={`删除费用项 ${index + 1} 的适用条件`}><X aria-hidden="true" /></button></div>)}</div>
                </article>
              ))}
            </section>
            <section className="account-metadata-editor" aria-labelledby="account-metadata-title">
              <div className="account-metadata-editor__heading"><div><h4 id="account-metadata-title">账户元数据</h4><p>使用字段填写账户补充信息；这些信息不会参与费用计算。</p></div></div>
              <div className="account-metadata-editor__grid">{ACCOUNT_METADATA_FIELDS.map((field) => <label key={field.key}><span>{field.label}<small className="field-help">{field.help}</small></span><input value={metadataValues[field.key] ?? ""} onChange={(event) => setMetadataValues((current) => ({ ...current, [field.key]: event.target.value }))} placeholder={field.placeholder} /></label>)}</div>
              <div className="account-metadata-editor__custom"><div className="account-metadata-editor__custom-heading"><div><strong>其他字段（Custom Metadata）</strong><small>如需保存额外信息，填写自定义名称和值；不再需要编辑 JSON。</small></div><button className="secondary-button" type="button" onClick={addMetadataRow}><Plus aria-hidden="true" />新增字段</button></div>{metadataRows.length === 0 ? <p className="account-metadata-editor__empty">暂无自定义字段。</p> : metadataRows.map((entry, index) => <div className="account-metadata-editor__row" key={index}><input aria-label={`自定义元数据名称 ${index + 1}`} value={entry.key} onChange={(event) => updateMetadataRow(index, { key: event.target.value })} placeholder="字段名称，如 strategy_group" /><span>=</span><input aria-label={`自定义元数据值 ${index + 1}`} value={entry.value} onChange={(event) => updateMetadataRow(index, { value: event.target.value })} placeholder="字段值" /><button className="icon-button" type="button" onClick={() => removeMetadataRow(index)} aria-label={`删除自定义元数据 ${index + 1}`}><X aria-hidden="true" /></button></div>)}</div>
            </section>
            <div className="prototype-modal__actions"><button className="secondary-button" type="button" onClick={() => setEditing(false)}>取消</button><button className="task-create-button" type="submit" disabled={saving || !draft.name.trim() || !draft.fee_schedule.key.trim()}>{saving && <LoaderCircle className="spin" aria-hidden="true" />}{saving ? "保存中" : "保存账户"}</button></div>
          </form>
        </div>
      )}
    </section>
  );
}
