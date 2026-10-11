import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { Select } from "../../components/controls/Select";
import { DatePicker } from "../../components/controls/DatePicker";
import { isDialogBackdropClick } from "../../components/controls/dialogBackdrop";
import { todayKey } from "../../components/controls/calendar";
import * as api from "../../api/runService";
import { jsonObject, modelName, validateConfig } from "./run-ui/config";
import "./run-ui/RunUI.css";

type Props = { initialStrategyId?: string; source?: api.RunSummary; onClose: () => void; onCreated: (run: api.RunSummary | api.BatchPage) => void };
export function CreateRunDrawer({ initialStrategyId, source, onClose, onCreated }: Props) {
  const saved = source?.normalized_config;
  const [caps, setCaps] = useState<api.Capabilities | null>(null);
  const [strategies, setStrategies] = useState<api.StrategyChoice[]>([]), [accounts, setAccounts] = useState<api.AccountChoice[]>([]);
  const [strategyOffset, setStrategyOffset] = useState(0), [accountOffset, setAccountOffset] = useState(0);
  const [moreStrategies, setMoreStrategies] = useState(false), [moreAccounts, setMoreAccounts] = useState(false);
  const [strategyId, setStrategyId] = useState(initialStrategyId || ""), [revisions, setRevisions] = useState<api.RevisionChoice[]>([]);
  const [revisionId, setRevisionId] = useState(saved?.strategy_revision_id || ""), [accountId, setAccountId] = useState(saved?.account_id || "");
  const [start, setStart] = useState(saved?.start || todayKey()), [end, setEnd] = useState(saved?.end || todayKey());
  const [cash, setCash] = useState(saved?.initial_cash || ""), [universe, setUniverse] = useState(saved?.universe.join("\n") || "");
  const [frequency, setFrequency] = useState<api.Frequency>(saved?.frequency || "1d"), [model, setModel] = useState<api.ExecutionModel>(saved?.execution_model || "bar_next_interval_v1");
  const [participation, setParticipation] = useState(saved?.participation_rate || "0.10"), [slippage, setSlippage] = useState(saved?.slippage_bps || "0");
  const [calendar, setCalendar] = useState(saved?.reference_calendar || ""), [benchmark, setBenchmark] = useState(saved?.benchmark || "");
  const [commission, setCommission] = useState(saved?.cost_overrides?.commission_rate || ""), [minimum, setMinimum] = useState(saved?.cost_overrides?.minimum_commission || "");
  const [seed, setSeed] = useState(String(saved?.seed ?? 0)), [riskFree, setRiskFree] = useState(saved?.risk_free_rate || "0");
  const [annual, setAnnual] = useState(String(saved?.annualization_sessions ?? 252)), [sampling, setSampling] = useState<"session_close" | "bar_close">(saved?.result_sampling || "session_close");
  const [parameters, setParameters] = useState(JSON.stringify(saved?.parameters || {}, null, 2)), [batch, setBatch] = useState(false), [variants, setVariants] = useState("[{}, {}]");
  const [loading, setLoading] = useState(true), [revisionLoading, setRevisionLoading] = useState(false), [checking, setChecking] = useState(false), [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState(""), [reports, setReports] = useState<api.Preflight[] | null>(null);
  const dialog = useRef<HTMLDialogElement>(null), generation = useRef(0), lock = useRef(false), active = useRef(true), preflightController = useRef<AbortController | null>(null);
  const checked = useRef<{ fingerprint: string; configs: api.RunConfig[] } | null>(null);
  useEffect(() => {
    active.current = true;
    const previous = document.activeElement as HTMLElement | null, overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden"; dialog.current?.showModal();
    dialog.current?.querySelector<HTMLElement>("[data-form-title]")?.focus();
    return () => { active.current = false; generation.current++; preflightController.current?.abort(); dialog.current?.close(); document.body.style.overflow = overflow; if (previous?.isConnected) previous.focus(); };
  }, []);
  useEffect(() => {
    const c = new AbortController(); setLoading(true);
    Promise.all([api.capabilities(c.signal), api.strategyPage(strategyOffset, c.signal), api.accountPage(accountOffset, c.signal)])
      .then(([capabilities, strategyPage, accountPage]) => {
        if (c.signal.aborted) return; setCaps(capabilities);
        setStrategies(old => [...new Map([...old, ...strategyPage].map(s => [s.id, s])).values()]);
        setAccounts(old => [...new Map([...old, ...accountPage.items].map(a => [a.id, a])).values()]);
        setMoreStrategies(strategyPage.length === 100); setMoreAccounts(accountOffset + accountPage.items.length < accountPage.total);

      }).catch(e => { if (!c.signal.aborted) setMessage(e instanceof Error ? e.message : "选项加载失败。"); })
      .finally(() => { if (!c.signal.aborted) setLoading(false); });
    return () => c.abort();
  }, [strategyOffset, accountOffset]);
  useEffect(() => {
    const c = new AbortController(); invalidate(); setRevisions([]); setRevisionLoading(!!strategyId);
    if (!strategyId) return () => c.abort();
    api.revisions(strategyId, c.signal).then(rows => {
      if (c.signal.aborted) return;
      setRevisions(rows); const chosen = rows.find(r => r.id === saved?.strategy_revision_id) || [...rows].sort((a,b) => b.revision_number-a.revision_number)[0];
      setRevisionId(chosen?.id || ""); setParameters(JSON.stringify(chosen?.id === saved?.strategy_revision_id ? saved.parameters : chosen?.default_parameters || {}, null, 2));
    }).catch(e => { if (!c.signal.aborted) setMessage(e.message); }).finally(() => { if (!c.signal.aborted) setRevisionLoading(false); });
    return () => c.abort();
  }, [strategyId]);
  function invalidate() { generation.current++; preflightController.current?.abort(); checked.current = null; setReports(null); setChecking(false); }
  function edit(change: () => void) { invalidate(); setMessage(""); change(); }
  function configs(): api.RunConfig[] {
    if (!caps) throw new Error("运行能力尚未加载，不能默认可运行。");
    if (!revisions.some(r => r.id === revisionId) || !accounts.some(a => a.id === accountId && a.status === "active")) throw new Error("策略修订或账户不可用，请重新选择。");
    if (!/^\d+$/.test(seed) || !/^\d+$/.test(annual)) throw new Error("随机种子与年化会话数须填写十进制整数，不能留空。");
    const cost = { ...(commission !== "" ? { commission_rate: commission } : {}), ...(minimum !== "" ? { minimum_commission: minimum } : {}) };
    const config: api.RunConfig = { api_schema: "qf.backtest.v2", strategy_revision_id: revisionId, account_id: accountId,
      initial_cash: cash, currency: "CNY", start, end, frequency, universe: universe.split(/[\s,，]+/).filter(Boolean),
      parameters: jsonObject(parameters), execution_model: model, participation_rate: participation, slippage_bps: slippage,
      risk_free_rate: riskFree, annualization_sessions: Number(annual), seed: Number(seed), result_sampling: sampling,
      ...(calendar ? { reference_calendar: calendar } : {}), ...(benchmark ? { benchmark } : {}), ...(Object.keys(cost).length ? { cost_overrides: cost } : {}) };
    let rows = [config];
    if (batch) {
      const array = JSON.parse(variants);
      if (!Array.isArray(array) || !array.length || array.length > caps.limits.batch_runs) throw new Error(`批次须包含 1–${caps.limits.batch_runs} 项显式参数对象。`);
      rows = array.map(p => ({ ...config, parameters: jsonObject(JSON.stringify(p)) }));
      // Check original text too, before rounding any unsafe integer.
      jsonObject(`{"items":${variants}}`);
    }
    rows.forEach(row => validateConfig(row, caps)); return rows;
  }
  async function check() {
    if (lock.current || checking) return;
    let rows: api.RunConfig[]; try { rows = configs(); } catch(e) { setMessage(e instanceof Error ? e.message : "配置无效。"); return; }
    const seq = ++generation.current, c = new AbortController(); preflightController.current?.abort(); preflightController.current = c;
    checked.current = null; setReports(null); setChecking(true); setMessage("");
    try {
      const fresh: api.Preflight[] = [];
      for (const row of rows) { fresh.push(await api.preflight(row, c.signal)); if (seq !== generation.current || c.signal.aborted) return; }
      setReports(fresh);
      if (fresh.every(r => r.errors.length === 0 && r.normalized_config)) checked.current = { fingerprint: api.canonical(rows), configs: fresh.map(r => r.normalized_config!) };
      setMessage(checked.current ? "当前配置预检通过。提交与执行时服务还会复核数据、权限及隔离条件。" : "预检未通过，请根据下列问题修改配置或补齐依赖。");
    } catch(e) { if (seq === generation.current && !c.signal.aborted) setMessage(e instanceof Error ? e.message : "预检失败。"); }
    finally { if (seq === generation.current) setChecking(false); }
  }
  async function create() {
    if (lock.current || !checked.current) return;
    let rows: api.RunConfig[]; try { rows = configs(); } catch(e) { setMessage(e instanceof Error ? e.message : "配置无效。"); return; }
    if (api.canonical(rows) !== checked.current.fingerprint) { invalidate(); return; }
    lock.current = true; setSubmitting(true); setMessage("");
    try { const value = await api.submit(batch ? { configs: checked.current.configs } : checked.current.configs[0]); if (active.current) onCreated(value); }
    catch(e) { if (active.current) setMessage(`${e instanceof Error ? e.message : "提交失败。"} 如结果待确认，请关闭表单后在工作台核对原提交。`); }
    finally { lock.current = false; if (active.current) setSubmitting(false); }
  }
  const actual = caps?.actual_data.filter(d => d.frequency === frequency);
  const revision = revisions.find(r => r.id === revisionId);
  const schemaProperties = revision?.parameter_schema.properties as Record<string, {type?: string; description?: string}> | undefined;
  return <dialog ref={dialog} className="qfb-drawer qf-create-drawer qf-create-wide qf-run-form" aria-labelledby="qfb-create-title" onCancel={e => { e.preventDefault(); onClose(); }} onKeyDown={e => {
    if (e.key !== "Tab" || (e.target as HTMLElement).closest("dialog") !== dialog.current) return;
    const items = [...e.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), [tabindex="0"], a[href]')].filter(node => node.getClientRects().length);
    const index = items.indexOf(document.activeElement as HTMLElement);
    if (items.length && (index < 0 || e.shiftKey && index === 0 || !e.shiftKey && index === items.length - 1)) { e.preventDefault(); items[e.shiftKey ? items.length - 1 : 0].focus(); }
  }} onClick={e => { if (isDialogBackdropClick(e)) onClose(); }}>
    <header><div><small>RUST BACKTEST</small><h2 id="qfb-create-title" data-form-title tabIndex={-1}>{source ? "按原配置重新预检" : "创建 Rust 回测"}</h2></div><button aria-label="关闭创建回测" onClick={onClose}><X size={18}/></button></header>
    <div className="qfb-drawer-body">
      {loading && <p role="status">正在加载能力与选项…</p>}
      {caps && <div className="qfb-note">引擎：{caps.engine.installed ? "制品已安装" : "制品尚未安装"}。{actual?.length ? actual.every(d => d.available) ? "所选频率已有可用数据映射，仍须预检。" : "所选频率的正式数据尚不可用。" : "所选频率的数据就绪状态尚未确认。"}{caps.actual_data.some(d => d.synthetic) && " 当前服务使用合成隔离数据，不证明生产可用。"}</div>}
      <fieldset disabled={loading || submitting}><legend>策略、账户与参数</legend>
        <label>策略<Select aria-label="策略" value={strategyId} disabled={!!initialStrategyId} onChange={e => edit(() => setStrategyId(e.target.value))}><option value="">请选择策略</option>{strategies.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</Select></label>
        <label>已发布策略修订<Select aria-label="已发布策略修订" value={revisionId} disabled={revisionLoading} onChange={e => edit(() => { setRevisionId(e.target.value); setParameters(JSON.stringify(revisions.find(r => r.id === e.target.value)?.default_parameters || {}, null, 2)); })}><option value="">请选择修订</option>{revisions.map(r => <option key={r.id} value={r.id}>v{r.revision_number} · {r.alias || "未命名"}</option>)}</Select></label>
        {!revisionLoading && strategyId && !revisions.length && <p>此策略尚无发布修订，请先在策略工作台发布。</p>}
        {moreStrategies && <button onClick={() => setStrategyOffset(v => v + 100)}>加载更多策略</button>}
        <label>回测账户<Select aria-label="回测账户" value={accountId} onChange={e => edit(() => setAccountId(e.target.value))}><option value="">请选择账户</option>{accounts.map(a => <option key={a.id} value={a.id}>{a.name} · 配置 v{a.version}</option>)}</Select></label>
        {moreAccounts && <button onClick={() => setAccountOffset(v => v + 20)}>加载更多账户</button>}
        <p>账户提供费用配置，服务受理时固定配置引用；初始资金必须另行填写。旧修订可保存和查看，执行前需要迁入 S3 回调接口。</p>
        <label>运行方式<Select aria-label="运行方式" value={batch ? "batch" : "single"} onChange={e => edit(() => setBatch(e.target.value === "batch"))}><option value="single">单次回测</option><option value="batch">参数批次</option></Select></label>
        <label className="qf-run-wide">{batch ? "参数批次 JSON" : "策略参数 JSON"}<textarea aria-label={batch ? "参数批次 JSON" : "策略参数 JSON"} rows={5} maxLength={batch ? 1024*1024 : 65536} value={batch ? variants : parameters} onChange={e => edit(() => batch ? setVariants(e.target.value) : setParameters(e.target.value))}/></label>
        {schemaProperties && <p className="qf-run-wide">策略参数：{Object.entries(schemaProperties).map(([key, value]) => `${key} (${value.type || "JSON"})`).join("、") || "未声明参数"}。批次每项使用同一策略、账户、日期与成本，服务逐项校验参数。</p>}
      </fieldset>
      <fieldset disabled={loading || submitting}><legend>日期、标的与资本</legend>
        <label>开始日期<DatePicker label="开始日期" value={start} maxDate={todayKey()} onChange={v => edit(() => setStart(v))}/></label>
        <label>结束日期<DatePicker label="结束日期" value={end} maxDate={todayKey()} onChange={v => edit(() => setEnd(v))}/></label>
        <label>初始资金（CNY）<input aria-label="初始资金（CNY）" inputMode="decimal" value={cash} onChange={e => edit(() => setCash(e.target.value))}/></label>
        <label>参考日历（可选）<input aria-label="参考日历" value={calendar} maxLength={128} onChange={e => edit(() => setCalendar(e.target.value))}/></label>
        <label className="qf-run-wide">标的代码集合<textarea aria-label="标的代码集合" rows={3} value={universe} onChange={e => edit(() => setUniverse(e.target.value))} placeholder="每行一个 A 股 / 境内 ETF 代码，例如 510300.SH"/></label>
        <p>标的集合是本次授权范围，历史身份与可交易性由服务核验。初始持仓为空；指数只能作为研究或基准，不能下单。</p>
      </fieldset>
      <fieldset disabled={loading || submitting}><legend>频率、撮合与成本假设</legend>
        <label>频率<Select aria-label="频率" value={frequency} onChange={e => edit(() => { const f = e.target.value as api.Frequency; setFrequency(f); setModel(f === "tick" ? "trade_tick_v1" : "bar_next_interval_v1"); if (f === "tick") setSampling("session_close"); })}><option value="">请选择频率</option>{caps?.engine.frequencies.map(f => <option key={f} value={f}>{f === "60m" ? "60m / 小时" : f}</option>)}</Select></label>
        <label>撮合模型<Select aria-label="撮合模型" value={model} onChange={e => edit(() => setModel(e.target.value as api.ExecutionModel))}>{caps?.engine.execution_models.filter(m => frequency === "tick" ? m !== "bar_next_interval_v1" : m === "bar_next_interval_v1").map(m => <option key={m} value={m}>{modelName[m] || m}</option>)}</Select></label>
        <label>模型参与率<input aria-label="模型参与率" value={participation} inputMode="decimal" onChange={e => edit(() => setParticipation(e.target.value))}/></label>
        <label>滑点（基点）<input aria-label="滑点（基点）" value={slippage} inputMode="decimal" onChange={e => edit(() => setSlippage(e.target.value))}/></label>
        <label>佣金率覆盖（可选）<input aria-label="佣金率覆盖" value={commission} inputMode="decimal" onChange={e => edit(() => setCommission(e.target.value))}/></label>
        <label>最低佣金覆盖（可选）<input aria-label="最低佣金覆盖" value={minimum} inputMode="decimal" onChange={e => edit(() => setMinimum(e.target.value))}/></label>
        <p>留空沿用账户佣金配置；法定税费由日期规则决定。零滑点是理想化模型假设，不表示零费用。参与率不保证实际成交。</p>
      </fieldset>
      <fieldset disabled={loading || submitting}><legend>统计假设</legend>
        <label>基准（可选）<input aria-label="基准" value={benchmark} onChange={e => edit(() => setBenchmark(e.target.value))}/></label>
        <label>随机种子<input aria-label="随机种子" value={seed} inputMode="numeric" onChange={e => edit(() => setSeed(e.target.value))}/></label>
        <label>年化无风险利率<input aria-label="年化无风险利率" value={riskFree} inputMode="decimal" onChange={e => edit(() => setRiskFree(e.target.value))}/></label>
        <label>年化会话数<input aria-label="年化会话数" value={annual} inputMode="numeric" onChange={e => edit(() => setAnnual(e.target.value))}/></label>
        <label>结果采样<Select aria-label="结果采样" value={sampling} onChange={e => edit(() => setSampling(e.target.value as typeof sampling))}><option value="session_close">会话收盘</option>{frequency !== "tick" && <option value="bar_close">Bar 收盘</option>}</Select></label>
        <p>默认年化基数 252、无风险利率 0 为明示统计假设；结果采样不改变交易时序。当前修订数据不等于历史时点数据库快照。</p>
      </fieldset>
      {message && <p className="qfb-form-message" role="status">{message}</p>}
      {reports && <section aria-label="预检问题">{reports.map((r, i) => <div key={i}><h3>{batch ? `参数 ${i+1}：` : ""}{r.errors.length ? "未通过" : "预检通过"}</h3>{r.errors.map((issue, n) => <p role="alert" key={n}>{api.issueText(issue)} <code>{api.issueCode(issue)}</code></p>)}{r.warnings.length > 0 && <p>本次使用当前修订数据；预检通过不保证执行期间数据保持不变。</p>}</div>)}</section>}
    </div>
    <footer><button onClick={onClose}>{submitting ? "关闭并稍后核对" : "取消"}</button><span/><button disabled={loading || checking || submitting || !caps?.engine.installed} onClick={() => void check()}>{checking ? "预检中…" : "预检当前配置"}</button><button className="qfb-primary" disabled={loading || submitting || !checked.current} onClick={() => void create()}>{submitting ? "提交中…" : batch ? "创建参数批次" : "提交运行"}</button></footer>
  </dialog>;
}
