import { useEffect, useRef, useState, type ReactNode } from "react";
import { LoaderCircle, Plus, Trash2, X } from "lucide-react";
import { AccountProfileApiError, createAccountProfile, getAccountProfile, updateAccountProfile, type AccountProfile, type AccountProfileStatus, type FeeRule } from "../../api/accountProfiles";
import { CATEGORIES, LEVELS, META, MODES, STATUS, changedPayload, hasStructuredMetadata, makeDraft, newRule, payloadFor, sideLabel, validateDraft, type Draft, type Issue, type Pair } from "./editor";

const STEPS = ["基础信息", "费用规则", "账户元数据", "确认"];
function Field({ label, id, help, issue, children }: { label: string; id: string; help?: string; issue: Issue | null; children: ReactNode }) {
  return <div className="qfac-field" data-field={id}><label htmlFor={`qfac-${id}`}>{label}</label>{children}{help && <small>{help}</small>}{issue?.field === id && <small className="qfac-field-error" role="alert">{issue.message}</small>}</div>;
}
function PairsEditor({ rows, onChange, label, disabled = false }: { rows: Pair[]; onChange: (rows: Pair[]) => void; label: string; disabled?: boolean }) {
  return <div className="qfac-pairs"><p className="qfac-help">{label === "适用条件" ? "按名称和值精确匹配；多条件须同时满足。不设置条件表示不按属性限制。" : "保存账户的静态补充资料；自定义字段没有预设说明。"}</p>{rows.map((row, index) => <div className="qfac-pair" key={index}>
    <label><span>{label}名称 {index + 1}</span><input disabled={disabled} value={row.key} onChange={e => onChange(rows.map((item, i) => i === index ? { ...item, key: e.target.value } : item))} /></label>
    <label><span>{label}值 {index + 1}</span><input disabled={disabled} value={row.value} onChange={e => onChange(rows.map((item, i) => i === index ? { ...item, value: e.target.value } : item))} /></label>
    <button className="qfac-icon" type="button" disabled={disabled} aria-label={`删除${label} ${index + 1}`} onClick={() => onChange(rows.filter((_, i) => i !== index))}><Trash2 /></button>
  </div>)}<button className="qfac-button" type="button" disabled={disabled} onClick={() => onChange([...rows, { key: "", value: "" }])}><Plus />新增{label === "适用条件" ? "条件" : "字段"}</button></div>;
}
export function AccountWizard({ source: initial, copy, onClose, onSaved }: { source?: AccountProfile; copy: boolean; onClose: () => void; onSaved: (profile: AccountProfile, message: string) => void }) {
  const [source, setSource] = useState(initial);
  const [draft, setDraft] = useState(() => makeDraft(initial, copy));
  const [step, setStep] = useState(0), [ruleIndex, setRuleIndex] = useState(0);
  const [issue, setIssue] = useState<Issue | null>(null), [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false), [conflict, setConflict] = useState(false);
  const [confirmation, setConfirmation] = useState<"close" | "reload" | null>(null);
  const dialog = useRef<HTMLDialogElement>(null), body = useRef<HTMLDivElement>(null), busyRef = useRef(false);
  const start = useRef(JSON.stringify(makeDraft(initial, copy)));
  const dirty = JSON.stringify(draft) !== start.current;
  const structured = hasStructuredMetadata(source);
  const isEdit = !!source && !copy;
  const rule = draft.rules[ruleIndex];
  useEffect(() => {
    const element = dialog.current!;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    element.showModal();
    return () => { element.close(); document.body.style.overflow = previousOverflow; previous?.focus(); };
  }, []);
  useEffect(() => {
    if (!dirty) return;
    const prevent = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", prevent);
    return () => window.removeEventListener("beforeunload", prevent);
  }, [dirty]);
  useEffect(() => {
    body.current?.scrollTo(0, 0);
    const target = confirmation ? dialog.current?.querySelector<HTMLElement>("[data-keep-editing]") : body.current?.querySelector<HTMLElement>(issue ? `[data-field="${issue.field}"] input, [data-field="${issue.field}"] select` : "input, select, [tabindex='-1']");
    target?.focus();
  }, [step, ruleIndex, issue, confirmation]);
  function close() { if (!busyRef.current) dirty ? setConfirmation("close") : onClose(); }
  function change(patch: Partial<Draft>) { setDraft(current => ({ ...current, ...patch })); setIssue(null); setError(null); }
  function changeRule(patch: Partial<typeof rule>) { change({ rules: draft.rules.map((item, index) => index === ruleIndex ? { ...item, ...patch } : item) }); }
  function metadataValue(key: string, value: string) {
    const exists = draft.metadata.some(row => row.key === key);
    change({ metadata: exists ? draft.metadata.map(row => row.key === key ? { key, value } : row) : [...draft.metadata, { key, value }] });
  }
  async function reload() {
    if (!source || busyRef.current) return;
    busyRef.current = true; setBusy(true);
    try {
      const latest = await getAccountProfile(source.id), fresh = makeDraft(latest);
      setSource(latest); setDraft(fresh); start.current = JSON.stringify(fresh);
      setConflict(false); setError(null); setIssue(null); setRuleIndex(0); setStep(0); setConfirmation(null);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "重新加载失败。"); setConfirmation(null); }
    finally { busyRef.current = false; setBusy(false); }
  }
  async function next() {
    if (busyRef.current) return;
    const invalid = validateDraft(draft);
    if (invalid && invalid.step <= step) { setIssue(invalid); setStep(invalid.step); if (invalid.rule != null) setRuleIndex(invalid.rule); return; }
    if (step < 3) { setStep(step + 1); setIssue(null); return; }
    if (copy && structured) { setError("原账户含非文本元数据，复制会丢失这些值。请先确认元数据兼容方案。"); return; }
    const patch = source ? changedPayload(draft, source) : null;
    if (isEdit && patch && Object.keys(patch).length === 1) { onClose(); return; }
    busyRef.current = true; setBusy(true); setError(null);
    try {
      const saved = isEdit && source && patch ? await updateAccountProfile(source.id, patch) : await createAccountProfile(payloadFor(draft, source));
      onSaved(saved, isEdit ? "账户已更新，历史回测仍保留原配置。" : "账户已创建。");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "保存失败，请重试。");
      setConflict(isEdit && caught instanceof AccountProfileApiError && caught.status === 409);
    } finally { busyRef.current = false; setBusy(false); }
  }
  const inputProps = (field: string) => ({ id: `qfac-${field}`, "aria-invalid": issue?.field === field || undefined });
  return <dialog ref={dialog} className="qfac-dialog" aria-labelledby="qfac-wizard-title" aria-modal="true" onKeyDown={event => {
      // Keep boundary Tab navigation inside the dialog even when Chromium
      // would otherwise move focus to browser chrome from the first control.
      if (event.key !== "Tab") return;
      const controls = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex="0"]')).filter(element => element.getClientRects().length > 0);
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }} onCancel={e => { e.preventDefault(); close(); }} onClick={e => { if (e.target === e.currentTarget) { const rect = e.currentTarget.getBoundingClientRect(); if (e.clientX < rect.left || e.clientX > rect.right || e.clientY < rect.top || e.clientY > rect.bottom) close(); } }}>
    <form onSubmit={e => { e.preventDefault(); void next(); }} noValidate>
      <header className="qfac-modal-head"><div><div className="qfac-eyebrow">{isEdit ? "EDIT ACCOUNT" : copy ? "COPY ACCOUNT" : "NEW ACCOUNT"}</div><h2 id="qfac-wizard-title">{isEdit ? "编辑回测账户" : copy ? "复制回测账户" : "新建回测账户"}</h2></div><button className="qfac-icon" type="button" disabled={busy} onClick={close} aria-label="关闭账户向导"><X /></button></header>
      <div className="qfac-summary"><div><span>账户名称</span><strong title={draft.name}>{draft.name || "未命名账户"}</strong></div><div><span>费用方案标识</span><strong title={draft.scheduleKey}>{draft.scheduleKey || "—"}</strong></div><div><span>状态</span><strong>{STATUS[draft.status]}</strong></div></div>
      <ol className="qfac-steps">{STEPS.map((label, index) => <li key={label} aria-current={index === step ? "step" : undefined} className={index < step ? "qfac-done" : ""}><b>{index + 1}</b><span>{label}</span></li>)}</ol>
      <div className="qfac-modal-body" ref={body}>
        {confirmation ? <section className="qfac-discard"><h3>{confirmation === "close" ? "放弃未保存的修改？" : "载入最新账户配置？"}</h3><p>{confirmation === "close" ? "关闭后，本次填写的内容不会保存。" : "当前填写的内容将被最新版本替换。"}</p><div className="qfac-actions"><button data-keep-editing className="qfac-button" type="button" disabled={busy} onClick={() => setConfirmation(null)}>继续编辑</button><button className="qfac-button qfac-primary" type="button" disabled={busy} onClick={() => confirmation === "close" ? onClose() : void reload()}>{confirmation === "close" ? "放弃修改" : "载入最新版本"}</button></div></section> : <fieldset disabled={busy}>
        {step === 0 && <>
          <h3>账户标识</h3><div className="qfac-form-grid">
            <Field label="账户名称" id="name" issue={issue} help="用于账户列表和回测选择器。"><input {...inputProps("name")} maxLength={100} value={draft.name} onChange={e => change({ name: e.target.value })} placeholder="例如：ETF 研究账户" /></Field>
            <Field label="费用方案标识" id="scheduleKey" issue={issue} help="标识本账户的费用方案，后续可编辑。"><input {...inputProps("scheduleKey")} maxLength={100} value={draft.scheduleKey} onChange={e => change({ scheduleKey: e.target.value })} placeholder="例如：etf-research" /></Field>
          </div>
          <h3>可用状态</h3><div className="qfac-choices">{(Object.keys(STATUS) as AccountProfileStatus[]).map(status => <label key={status} className={draft.status === status ? "qfac-selected" : ""}><input type="radio" name="account-status" checked={draft.status === status} onChange={() => change({ status })} /><strong>{STATUS[status]}</strong><small>{status === "active" ? "可用于新的回测运行" : status === "inactive" ? "暂不用于新的回测" : "保留历史配置与记录"}</small></label>)}</div>
          <h3>账户资料</h3>{structured && <p className="qfac-note">此账户含非文本元数据，暂以只读方式保留；其他账户及费用字段仍可编辑。</p>}<div className="qfac-form-grid">{Object.entries(META).map(([key, label]) => <Field key={key} label={label} id={`meta-${key}`} issue={issue}><input id={`qfac-meta-${key}`} disabled={structured} value={draft.metadata.find(row => row.key === key)?.value ?? ""} onChange={e => metadataValue(key, e.target.value)} /></Field>)}</div>
          {isEdit && <p className="qfac-help qfac-break">账户 UUID：{source?.id}<br />当前账户 v{source?.version} · 费用 v{source?.fee_schedule_version}</p>}
        </>}
        {step === 1 && <div className="qfac-fee-editor">
          <nav className="qfac-rule-nav" aria-label="选择费用规则">{draft.rules.map((item, index) => <button type="button" key={index} aria-pressed={index === ruleIndex} onClick={() => { setRuleIndex(index); setIssue(null); }}><strong>{CATEGORIES[item.category] ?? item.category ?? "费用规则"} {index + 1}</strong><small>{sideLabel(item.side)}</small></button>)}<button className="qfac-add-rule" type="button" onClick={() => { let key = `fee_${draft.rules.length + 1}`; while (draft.rules.some(item => item.key === key)) key += "_new"; change({ rules: [...draft.rules, newRule(key)] }); setRuleIndex(draft.rules.length); }}><Plus />新增费用规则</button></nav>
          <div className="qfac-rule-form"><header><h3>费用规则 {ruleIndex + 1}</h3><button className="qfac-icon" type="button" disabled={draft.rules.length === 1} onClick={() => { change({ rules: draft.rules.filter((_, i) => i !== ruleIndex) }); setRuleIndex(Math.max(0, ruleIndex - 1)); }} aria-label="删除当前费用规则"><Trash2 /></button></header>
            <div className="qfac-form-grid">
              <Field label="规则标识" id="key" issue={issue}><input {...inputProps("key")} maxLength={100} value={rule.key} onChange={e => changeRule({ key: e.target.value })} /></Field>
              <Field label="费用类型" id="category" issue={issue}><select {...inputProps("category")} value={rule.category} onChange={e => changeRule({ category: e.target.value })}>{!CATEGORIES[rule.category] && <option value={rule.category}>兼容类型（{rule.category}）</option>}{Object.entries(CATEGORIES).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></Field>
              <Field label="适用方向" id="side" issue={issue}><select id="qfac-side" value={rule.side ?? ""} onChange={e => changeRule({ side: e.target.value || null })}>{rule.side && !["buy", "sell"].includes(rule.side) && <option value={rule.side}>{sideLabel(rule.side)}</option>}<option value="">买入和卖出</option><option value="buy">仅买入</option><option value="sell">仅卖出</option></select></Field>
              {([ ["rate", "费率", "0.03% = 0.0003"], ["minimum", "最低费用", "0 表示无最低费用"], ["fixed_amount", "固定费用", "金额使用账户结算币种"] ] as const).map(([key, label, help]) => <Field key={key} label={label} id={key} issue={issue} help={help}><input {...inputProps(key)} inputMode="decimal" value={rule[key]} onChange={e => changeRule({ [key]: e.target.value })} /></Field>)}
              <Field label="取整层级" id="rounding_level" issue={issue}><select {...inputProps("rounding_level")} value={rule.rounding_level ?? ""} onChange={e => changeRule({ rounding_level: e.target.value as FeeRule["rounding_level"] })}><option value="" disabled>请选择</option>{Object.entries(LEVELS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></Field>
              <Field label="取整范围" id="rounding_scope" issue={issue}><input {...inputProps("rounding_scope")} maxLength={100} value={rule.rounding_scope ?? ""} onChange={e => changeRule({ rounding_scope: e.target.value })} /></Field>
              <Field label="取整方式" id="rounding_mode" issue={issue}><select {...inputProps("rounding_mode")} value={rule.rounding_mode ?? ""} onChange={e => changeRule({ rounding_mode: e.target.value as FeeRule["rounding_mode"] })}><option value="" disabled>请选择</option>{Object.entries(MODES).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></Field>
              <Field label="取整精度" id="rounding_precision" issue={issue} help="例如 0.01；须大于 0。"><input {...inputProps("rounding_precision")} inputMode="decimal" value={rule.rounding_precision ?? ""} onChange={e => changeRule({ rounding_precision: e.target.value })} /></Field>
            </div><div data-field="conditions"><h3>适用条件（可选）</h3>{issue?.field === "conditions" && <p className="qfac-field-error" role="alert">{issue.message}</p>}<PairsEditor label="适用条件" rows={rule.rows} onChange={rows => changeRule({ rows })} /></div>
          </div>
        </div>}
        {step === 2 && <div data-field="metadata"><h3 tabIndex={-1}>账户元数据</h3>{structured ? <><p className="qfac-note">已有非文本值将原样保留。本次编辑不替换元数据。</p><pre>{JSON.stringify(source?.metadata, null, 2)}</pre></> : <><p className="qfac-help">基础资料与自定义字段统一保存在账户元数据中。</p>{issue?.field === "metadata" && <p className="qfac-field-error" role="alert">{issue.message}</p>}<PairsEditor label="元数据" rows={draft.metadata} onChange={metadata => change({ metadata })} /></>}</div>}
        {step === 3 && <><h3 tabIndex={-1}>确认账户配置</h3><dl className="qfac-properties"><div><dt>账户名称</dt><dd>{draft.name}</dd></div><div><dt>费用方案标识</dt><dd>{draft.scheduleKey}</dd></div><div><dt>状态</dt><dd>{STATUS[draft.status]}</dd></div><div><dt>费用规则</dt><dd>{draft.rules.length} 条</dd></div></dl><h3>费用规则摘要</h3>{draft.rules.map((item, index) => <article className="qfac-review-rule" key={index}><strong>{index + 1}. {CATEGORIES[item.category] ?? item.category} · {item.key}</strong><p>{sideLabel(item.side)} · 费率 {item.rate} · 最低 {item.minimum} · 固定 {item.fixed_amount}</p><p>{item.rounding_level && LEVELS[item.rounding_level]} / {item.rounding_scope} · {item.rounding_mode && MODES[item.rounding_mode]} · 精度 {item.rounding_precision}</p><p>条件：{item.rows.length ? item.rows.map(row => `${row.key} = ${JSON.stringify(row.value)}`).join("；") : "无属性限制"}</p></article>)}<h3>账户元数据</h3><dl className="qfac-properties">{Object.entries(structured ? source!.metadata : payloadFor(draft).metadata).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{typeof value === "string" ? value || "（空字符串）" : JSON.stringify(value)}</dd></div>)}</dl><p className="qfac-note">{isEdit ? "保存修改将生成新账户版本，历史回测继续保留原版本配置。" : "保存后创建独立账户；配置和历史回测使用记录不会自动关联。"}</p></>}
        </fieldset>}
      </div>
      <footer className="qfac-modal-footer">{error && <div className="qfac-error" role="alert">{error}{conflict && <button type="button" className="qfac-button" disabled={busy} onClick={() => setConfirmation("reload")}>重新加载账户</button>}</div>}{!confirmation && <div className="qfac-footer-row"><span>第 {step + 1} 步，共 4 步</span><div className="qfac-actions"><button className="qfac-button" type="button" disabled={busy} onClick={step ? () => { setStep(step - 1); setIssue(null); } : close}>{step ? "上一步" : "取消"}</button><button className="qfac-button qfac-primary" type="submit" disabled={busy}>{busy && <LoaderCircle className="spin" />}{busy ? "保存中…" : step === 3 ? isEdit ? "保存修改" : "创建账户" : "下一步"}</button></div></div>}</footer>
    </form>
  </dialog>;
}
