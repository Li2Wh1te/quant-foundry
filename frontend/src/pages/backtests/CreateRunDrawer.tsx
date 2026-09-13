import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { getAccountPage, listAccountProfileVersions, type AccountProfile } from "../../api/accountProfiles";
import { createBacktestRun, fetchStrategyBacktestWorkspace, listFeeSchedules, type FeeCatalogVersion, type BacktestRun, type BacktestRunCreateInput, type ComponentDescriptor, type ComponentSelectionInput } from "../../api/backtestRuns";
import { listStrategies, type StrategySummary } from "../../api/strategies";
import { preflightBacktest } from "../../api/backtestPreflight";
import { admissionAllowsCreation } from "../../components/backtestAdmission";
import { copyConfiguration, configurationFingerprint } from "./workbench";
const today = new Date().toISOString().slice(0, 10);
type PriceBasis = "raw" | "qfq" | "hfq";
type JsonSchemaProperty = {
  title?: string;
  type?: string;
  enum?: unknown[];
  default?: unknown;
};
type PublishedRevision = {
  id: string;
  revision_number: number;
  source_hash: string;
  parameter_schema?: { properties?: Record<string, JsonSchemaProperty> };
  default_parameters?: Record<string, unknown>;
};
type PositionDraft = {
  instrument_id: string;
  quantity: string;
  available_quantity: string;
  average_price: string;
};

const emptyPosition = (): PositionDraft => ({
  instrument_id: "",
  quantity: "",
  available_quantity: "",
  average_price: "",
});

function schemaDefaults(schema: Record<string, unknown>): Record<string, unknown> {
  const properties = (schema.properties || {}) as Record<string, JsonSchemaProperty>;
  return Object.fromEntries(
    Object.entries(properties)
      .filter(([, definition]) => definition.default !== undefined)
      .map(([key, definition]) => [key, definition.default]),
  );
}

function parseParameterValue(definition: JsonSchemaProperty, value: string, checked: boolean): unknown {
  if (definition.type === "boolean") return checked;
  if (definition.type === "integer") return value === "" ? "" : Number.parseInt(value, 10);
  if (definition.type === "number") return value === "" ? "" : Number(value);
  return value;
}

export function CreateRunDrawer({ initialStrategyId, source, onClose, onCreated }: { initialStrategyId?: string; source?: BacktestRun; onClose: () => void; onCreated: (run: BacktestRun) => void }) {
  const [revisions, setRevisions] = useState<PublishedRevision[]>([]);
  const [revisionId, setRevisionId] = useState("");
  const [schema, setSchema] = useState<Record<string, unknown>>({});
  const [parameters, setParameters] = useState<Record<string, unknown>>({});
  const [startDate, setStartDate] = useState(today);
  const [endDate, setEndDate] = useState(today);
  const [initialCash, setInitialCash] = useState("0");
  const [positions, setPositions] = useState<PositionDraft[]>([]);
  const [dynamicUniverse] = useState(false);
  const [instrumentIds, setInstrumentIds] = useState("");
  const [exchanges, setExchanges] = useState<string[]>(["SSE", "SZSE"]);
  const [priceBasis, setPriceBasis] = useState<PriceBasis>("raw");
  const [warmupSessions, setWarmupSessions] = useState("0");
  const [randomSeed, setRandomSeed] = useState("");
  const [accounts, setAccounts] = useState<AccountProfile[]>([]);
  const [accountProfileId, setAccountProfileId] = useState("");
  const [slippageModels, setSlippageModels] = useState<ComponentDescriptor[]>([]);
  const [slippageIdentity, setSlippageIdentity] = useState("");
  const [slippageParameters, setSlippageParameters] = useState<Record<string, unknown>>({});
  const [accountVersions, setAccountVersions] = useState<AccountProfile[]>([]);
  const [componentOptions, setComponentOptions] = useState<Record<string, Array<ComponentSelectionInput & { display_name: string }>>>({});
  const [componentSelections, setComponentSelections] = useState<Record<string, ComponentSelectionInput>>({});
  const [feeVersions, setFeeVersions] = useState<FeeCatalogVersion[]>([]);
  const [feeIdentity, setFeeIdentity] = useState("");
  const [accountVersion, setAccountVersion] = useState("");
  const [sharpeMode, setSharpeMode] = useState("sharpe_simple");
  const [riskFreeRate, setRiskFreeRate] = useState("0");
  const [riskFreeNote, setRiskFreeNote] = useState("");
  const [busy, setBusy] = useState(false);
  const inputGeneration = useRef(0), workspaceGeneration = useRef(0);
  const admissionFingerprint = useRef<string | null>(null), creationKey = useRef<string | null>(null);
  const [message, setMessage] = useState("");
  const [preflight, setPreflight] = useState<Record<string, any> | null>(null);
  const [gate, setGate] = useState<Record<string, any> | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const resetAdmission = () => {
    inputGeneration.current += 1;
    admissionFingerprint.current = null;
    creationKey.current = null;
    setGate(null);
    setPreflight(null);
    setConfirmed(false);
  };

  const selectRevision = (id: string, available = revisions) => {
    const revision = available.find((item) => item.id === id);
    setRevisionId(id);
    setSchema((revision?.parameter_schema || {}) as Record<string, unknown>);
    setParameters({ ...(revision?.default_parameters || {}) });
    resetAdmission();
  };

  const selectSlippage = (identity: string, available = slippageModels) => {
    const selectedModel = available.find(
      (item) => `${item.key}@${item.version}` === identity,
    );
    setSlippageIdentity(identity);
    setSlippageParameters(schemaDefaults(selectedModel?.parameter_schema || {}));
    resetAdmission();
  };


  const [strategyId, setStrategyId] = useState(initialStrategyId || "");
  const [strategies, setStrategies] = useState<StrategySummary[]>([]);
  const [catalogLoading, setCatalogLoading] = useState(true), [workspaceLoading, setWorkspaceLoading] = useState(false), [step, setStep] = useState(0);
  const loading = catalogLoading || workspaceLoading;
  const [discard, setDiscard] = useState(false), [dirty, setDirty] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null), body = useRef<HTMLDivElement>(null);
  const copy = useRef(source ? copyConfiguration(source) : null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden"; dialog.current?.showModal();
    return () => { dialog.current?.close(); document.body.style.overflow = overflow; previous?.focus(); };
  }, []);
  useEffect(() => {
    body.current?.scrollTo(0, 0);
    body.current?.querySelector<HTMLElement>("[data-step-title]")?.focus();
  }, [step]);
  useEffect(() => {
    if (!dirty) return;
    const prevent = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", prevent);
    return () => window.removeEventListener("beforeunload", prevent);
  }, [dirty]);
  useEffect(() => {
    let active = true;
    (async () => {
      const all: StrategySummary[] = [];
      for (let offset = 0; ; offset += 500) {
        const page = await listStrategies(true, offset, 500);
        if (!active) return;
        all.push(...page); if (page.length < 500) break;
      }
      setStrategies(all);
      const accounts: AccountProfile[] = [];
      for (let offset = 0; ; ) {
        const page = await getAccountPage("", "", offset);
        if (!active) return;
        accounts.push(...page.items); offset += page.items.length;
        if (offset >= page.total || !page.items.length) break;
      }
      const fees = await listFeeSchedules();
      if (!active) return;
      setAccounts(accounts); setFeeVersions(fees); setCatalogLoading(false);
    })().catch(error => { if (active) { setMessage(error.message); setCatalogLoading(false); } });
    return () => { active = false; };
  }, []);
  useEffect(() => {
    const generation = ++workspaceGeneration.current;
    setRevisions([]); setRevisionId(""); resetAdmission();
    if (!strategyId) { setWorkspaceLoading(false); return; }
    setWorkspaceLoading(true);
    fetchStrategyBacktestWorkspace(strategyId).then(workspace => {
      if (generation !== workspaceGeneration.current) return;
      const available = workspace.published_revisions as PublishedRevision[];
      setRevisions(available); setSlippageModels(workspace.slippage_models);
      setComponentOptions(workspace.component_options || {});
      const saved = copy.current;
      selectRevision(saved?.strategy_revision_id || [...available].sort((a,b) => b.revision_number-a.revision_number)[0]?.id || "", available);
      const model = workspace.slippage_models.find(m => m.key === "none") || workspace.slippage_models[0];
      if (model) selectSlippage(`${model.key}@${model.version}`, workspace.slippage_models);
      setComponentSelections(Object.fromEntries(Object.entries(workspace.component_options || {}).filter(([, choices]) => choices.length).map(([kind, choices]) => [kind, { key: choices[0].key, version: choices[0].version, parameters: choices[0].parameters }])));
      if (saved) {
        const config = saved.backtest_config;
        setParameters(saved.parameters || {}); setStartDate(config.start_date); setEndDate(config.end_date);
        setInitialCash(String(config.initial_cash)); setPositions(config.initial_positions.map(p => ({ instrument_id: p.instrument_id, quantity: String(p.quantity), available_quantity: String(p.available_quantity), average_price: p.average_price == null ? "" : String(p.average_price) })));
        setInstrumentIds(config.instrument_ids.join("\n")); setExchanges(config.exchanges); setPriceBasis(config.strategy_price_bases[0]); setWarmupSessions(String(config.warmup_sessions));
        setAccountProfileId(saved.account_profile_id || ""); setRandomSeed(saved.random_seed == null ? "" : String(saved.random_seed));
        setSlippageIdentity(`${saved.slippage_model.key}@${saved.slippage_model.version}`); setSlippageParameters(saved.slippage_model.parameters);
        setComponentSelections(saved.component_selections || {});
        const sharpe = saved.analyzer_selections?.find(item => item.key.startsWith("sharpe_"));
        if (sharpe) { setSharpeMode(sharpe.key); setRiskFreeRate(String(sharpe.parameters.rf_annual ?? "0")); setRiskFreeNote(String(sharpe.parameters.rf_source_note ?? "")); }
        setFeeIdentity(saved.fee_schedule_selection ? `${saved.fee_schedule_selection.key}@${saved.fee_schedule_selection.version}` : "");
      }
    }).catch(error => { if (generation === workspaceGeneration.current) setMessage(error.message); })
      .finally(() => { if (generation === workspaceGeneration.current) setWorkspaceLoading(false); });
    return () => { workspaceGeneration.current += 1; inputGeneration.current += 1; };
  }, [strategyId]);
  useEffect(() => {
    let active = true; setAccountVersions([]); setAccountVersion(""); resetAdmission();
    if (accountProfileId) listAccountProfileVersions(accountProfileId).then(versions => {
      if (!active) return;
      setAccountVersions(versions);
      const requested = copy.current?.account_profile_id === accountProfileId ? copy.current.account_profile_version : null;
      setAccountVersion(String(requested || versions.find(v => v.status === "active")?.version || ""));
    }).catch(error => { if (active) setMessage(error.message); });
    return () => { active = false; };
  }, [accountProfileId]);
  useEffect(() => { if (message) body.current?.querySelector<HTMLElement>(".qfb-form-message")?.scrollIntoView({ block: "nearest" }); }, [message]);
  function close() { if (!busy) dirty ? setDiscard(true) : onClose(); }
  function next() {
    setMessage("");
    if (step === 0 && !revisionId) { setMessage("请选择已发布的策略版本。"); return; }
    if (step === 1 && !buildPayload(crypto.randomUUID())) return;
    setStep(step + 1);
  }
  const selectedSlippage = slippageModels.find(
    (item) => `${item.key}@${item.version}` === slippageIdentity,
  );

  const buildPayload = (idempotencyKey: string): BacktestRunCreateInput | null => {
    const fixedInstrumentIds = [
      ...new Set(
        instrumentIds
          .split(/[\s,，]+/)
          .map((value) => value.trim())
          .filter(Boolean),
      ),
    ];
    const nonEmptyPositions = positions.filter((item) =>
      Object.values(item).some((value) => value.trim()),
    );
    const requiredSlippageParameters = (
      selectedSlippage?.parameter_schema.required || []
    ) as string[];

    if (!revisionId) {
      setMessage("请先选择已发布策略版本。");
      return null;
    }
    if (!accountProfileId || !accountVersion || !accounts.some(a => a.id === accountProfileId && a.status === "active") || !accountVersions.some(a => String(a.version) === accountVersion && a.status === "active")) {
      setMessage("请选择可用的回测账户版本。");
      return null;
    }
    if (!revisions.some(r => r.id === revisionId)) { setMessage("原策略版本已不可用，请重新选择。"); return null; }
    if (feeIdentity && !feeVersions.some(f => `${f.key}@${f.version}` === feeIdentity)) { setMessage("原费用方案已不可用，请重新选择。"); return null; }
    if (Object.entries(componentSelections).some(([kind, choice]) => !componentOptions[kind]?.some(option => option.key === choice.key && option.version === choice.version))) { setMessage("原执行组件已不可用，请重新选择。"); return null; }
    if (!/^\d+$/.test(warmupSessions) || Number(warmupSessions) > 512) { setMessage("预热会话数必须是 0–512 的整数。"); return null; }
    if (!startDate || !endDate || startDate > endDate) {
      setMessage("请选择有效的回测起止日期。");
      return null;
    }
    if (!initialCash.trim()) {
      setMessage("请填写初始资金。");
      return null;
    }
    if (!exchanges.length) {
      setMessage("请至少选择一个交易所日历。");
      return null;
    }
    if (!dynamicUniverse && !fixedInstrumentIds.length && !nonEmptyPositions.length) {
      setMessage("固定数据范围至少需要一个标的或初始持仓。");
      return null;
    }
    if (!selectedSlippage) {
      setMessage("请选择滑点模型。");
      return null;
    }
    if (
      randomSeed.trim() &&
      (!/^-?\d+$/.test(randomSeed.trim()) ||
        !Number.isSafeInteger(Number(randomSeed)))
    ) {
      setMessage("随机种子必须是安全整数。");
      return null;
    }
    if (
      requiredSlippageParameters.some(
        (key) => String(slippageParameters[key] ?? "").trim() === "",
      )
    ) {
      setMessage("请填写滑点模型的必填参数。");
      return null;
    }
    if (
      nonEmptyPositions.some(
        (item) =>
          !item.instrument_id.trim() ||
          !item.quantity.trim() ||
          !item.available_quantity.trim() ||
          (Number(item.quantity) !== 0 && !item.average_price.trim()),
      )
    ) {
      setMessage("请完整填写每一条初始持仓。");
      return null;
    }

    return {
      strategy_revision_id: revisionId,
      parameters,
      backtest_config: {
        start_date: startDate,
        end_date: endDate,
        initial_cash: initialCash,
        initial_positions: nonEmptyPositions.map((item) => ({
          instrument_id: item.instrument_id.trim(),
          side: "long",
          quantity: item.quantity,
          available_quantity: item.available_quantity,
          average_price: item.average_price.trim() || null,
        })),
        dynamic_universe: dynamicUniverse,
        instrument_ids: fixedInstrumentIds,
        exchanges,
        strategy_price_bases: [priceBasis],
        currency: "CNY",
        timezone: "Asia/Shanghai",
        frequency: "1d",
        warmup_sessions: Number.parseInt(warmupSessions || "0", 10),
      },
      account_profile_id: accountProfileId,
      data_cutoff: typeof preflight?.data_cutoff === "string" ? preflight.data_cutoff : undefined,
      account_profile_version: Number(accountVersion),
      fee_schedule_selection: feeVersions.find(item => `${item.key}@${item.version}` === feeIdentity)
        ? { key: feeVersions.find(item => `${item.key}@${item.version}` === feeIdentity)!.key,
            version: feeVersions.find(item => `${item.key}@${item.version}` === feeIdentity)!.version, parameters: {} } : undefined,
      component_selections: componentSelections,
      analyzer_selections: [
        { key: sharpeMode, version: 1, parameters: sharpeMode === "sharpe_config_rf" ? { rf_annual: riskFreeRate, rf_source_note: riskFreeNote } : {} },
        ...["performance", "turnover", "fee_summary"].map((key) => ({ key, version: 1, parameters: {} })),
      ],
      slippage_model: {
        key: selectedSlippage.key,
        version: selectedSlippage.version,
        parameters: slippageParameters,
      },
      random_seed: randomSeed.trim() === "" ? null : Number(randomSeed),
      idempotency_key: idempotencyKey,
      degraded: confirmed,
      confirmed_admission_report_hash:
        confirmed && typeof preflight?.report_hash === "string"
          ? preflight.report_hash
          : null,
    };
  };

  const fingerprint = (payload: BacktestRunCreateInput) => configurationFingerprint(payload);

  const checkAdmission = async () => {
    if (busy) return;
    const generation = inputGeneration.current;
    const pageGeneration = workspaceGeneration.current;
    const payload = buildPayload(crypto.randomUUID());
    if (!payload || !accountVersion) return;
    setBusy(true); setGate(null);
    admissionFingerprint.current = null;
    try {
      const admission = await preflightBacktest(payload) as Record<string, any>;
      if (generation !== inputGeneration.current) return;
      setPreflight(admission);
      const allowed = admissionAllowsCreation(admission);
      if (admission.report_hash !== payload.confirmed_admission_report_hash) setConfirmed(false);
      setGate({ ...admission.gates, status: admission.status, allowed });
      admissionFingerprint.current = fingerprint(payload);
      setMessage(allowed ? "当前配置已通过预检，可以创建运行。" : admission.status === "degraded" ? "请勾选降级报告确认，再次预检以完成全部准入检查。" : "当前配置未通过预检，请查看问题并修改配置。");
    } catch (error) { if (generation === inputGeneration.current) setMessage(error instanceof Error ? error.message : "预检失败。"); }
    finally { if (pageGeneration === workspaceGeneration.current) setBusy(false); }
  };

  const create = async () => {
    if (busy) return;
    const pageGeneration = workspaceGeneration.current;
    const generation = inputGeneration.current;
    const key = creationKey.current || crypto.randomUUID();
    const payload = buildPayload(key);
    if (!payload || !gate?.allowed || admissionFingerprint.current !== fingerprint(payload) || (preflight?.status === "degraded" && !confirmed)) {
      setMessage("请先完成当前配置的预检及必要确认。"); return;
    }
    creationKey.current = key; setBusy(true);
    try {
      const run = await createBacktestRun(payload, key) as BacktestRun;
      if (pageGeneration !== workspaceGeneration.current) return;
      onCreated(run);
      if (generation === inputGeneration.current) { creationKey.current = null; setMessage("正式回测已加入队列。"); }
    } catch (error) {
      if (generation === inputGeneration.current) setMessage(error instanceof Error ? error.message : "创建回测失败。");
    } finally { if (pageGeneration === workspaceGeneration.current) setBusy(false); }
  };

  const strategyProperties = (schema.properties || {}) as Record<
    string,
    JsonSchemaProperty
  >;
  const slippageProperties = (selectedSlippage?.parameter_schema.properties || {}) as Record<
    string,
    JsonSchemaProperty
  >;

  return (
    <dialog ref={dialog} className="qfb-drawer" aria-labelledby="qfb-create-title" onCancel={event => { event.preventDefault(); close(); }}>
      <header><div><small>CREATE BACKTEST</small><h2 id="qfb-create-title">{source ? "复制配置" : "创建回测"}</h2></div><button aria-label="关闭创建回测" onClick={close} disabled={busy}><X size={18} /></button></header>
      {discard ? <div className="qfb-discard"><h3>放弃尚未提交的配置？</h3><p>关闭后，本次填写的内容不会保存。</p><button autoFocus onClick={() => setDiscard(false)}>继续编辑</button><button onClick={onClose}>放弃并关闭</button></div> : <>
      <ol className="qfb-steps">{["策略与版本", "账户与执行配置", "运行检查与确认"].map((name,index) => <li key={name} aria-current={index === step ? "step" : undefined}><span>{index+1}</span>{name}</li>)}</ol>
      <div className="qfb-drawer-body" ref={body} onChange={() => setDirty(true)}>
      <h3 data-step-title tabIndex={-1}>{["选择策略与版本", "配置账户、数据与执行", "运行检查与确认"][step]}</h3>
      {loading && <p role="status">正在加载可用配置…</p>}
      {step === 0 && <label>策略<select aria-label="策略" disabled={!!initialStrategyId || busy} value={strategyId} onChange={e => { copy.current = null; setStrategyId(e.target.value); }}><option value="">请选择策略</option>{strategies.map(s => <option key={s.id} value={s.id}>{s.name}{s.state === "archived" ? "（已归档）" : ""}</option>)}</select></label>}
      {!loading && step === 0 && strategyId && !revisions.length && <p>此策略尚未发布版本，请先前往策略工作台发布。</p>}
      {!loading && step === 1 && !accounts.some(a => a.status === "active") && <p role="alert">暂无可选择的回测账户。请先在回测账户页面创建账户。</p>}
      <fieldset disabled={busy || loading} hidden={step !== 0}>
        <legend>策略与参数</legend>
        <label>
          已发布版本
          <select
            aria-label="已发布策略版本"
            value={revisionId}
            onChange={(event) => selectRevision(event.target.value)}
          >
            <option value="">请选择版本</option>
            {revisions.map((revision) => (
              <option key={revision.id} value={revision.id}>
                版本 {revision.revision_number} · {revision.source_hash.slice(0, 12)}
              </option>
            ))}
          </select>
        </label>
        {Object.entries(strategyProperties).map(([key, definition]) => {
          const value = parameters[key];
          if (Array.isArray(definition.enum)) {
            return (
              <label key={key}>
                {String(definition.title || key)}
                <select
                  value={String(value ?? "")}
                  onChange={(event) => {
                    setParameters((current) => ({
                      ...current,
                      [key]: parseParameterValue(
                        definition,
                        event.target.value,
                        false,
                      ),
                    }));
                    resetAdmission();
                  }}
                >
                  {definition.enum.map((option) => (
                    <option key={String(option)} value={String(option)}>
                      {String(option)}
                    </option>
                  ))}
                </select>
              </label>
            );
          }
          return (
            <label key={key}>
              {String(definition.title || key)}
              <input
                aria-label={String(definition.title || key)}
                type={definition.type === "boolean" ? "checkbox" : definition.type === "string" ? "text" : "number"}
                checked={definition.type === "boolean" ? Boolean(value) : undefined}
                value={definition.type === "boolean" ? undefined : String(value ?? "")}
                onChange={(event) => {
                  setParameters((current) => ({
                    ...current,
                    [key]: parseParameterValue(
                      definition,
                      event.target.value,
                      event.target.checked,
                    ),
                  }));
                  resetAdmission();
                }}
              />
            </label>
          );
        })}
      </fieldset>

      <fieldset disabled={busy || loading} hidden={step !== 1}>
        <legend>日期、资金与数据范围</legend>
        <label>
          开始日期
          <input type="date" value={startDate} onChange={(event) => { setStartDate(event.target.value); resetAdmission(); }} />
        </label>
        <label>
          结束日期
          <input type="date" value={endDate} onChange={(event) => { setEndDate(event.target.value); resetAdmission(); }} />
        </label>
        <label>
          初始资金（CNY）
          <input inputMode="decimal" value={initialCash} onChange={(event) => { setInitialCash(event.target.value); resetAdmission(); }} />
        </label>
        <label>
          预热交易会话数
          <input type="number" min="0" max="512" value={warmupSessions} onChange={(event) => { setWarmupSessions(event.target.value); resetAdmission(); }} />
        </label>
        <p>选择本次回测使用的固定标的范围。</p>
        <label>
          固定标的 UUID（逗号或换行分隔）
          <textarea value={instrumentIds} onChange={(event) => { setInstrumentIds(event.target.value); resetAdmission(); }} />
        </label>
        <div>
          交易日历：
          {["SSE", "SZSE"].map((exchange) => (
            <label key={exchange}>
              <input
                type="checkbox"
                checked={exchanges.includes(exchange)}
                onChange={(event) => {
                  setExchanges((current) =>
                    event.target.checked
                      ? [...new Set([...current, exchange])]
                      : current.filter((item) => item !== exchange),
                  );
                  resetAdmission();
                }}
              />
              {exchange}
            </label>
          ))}
        </div>
        <label>
          策略查询价格口径
          <select value={priceBasis} onChange={(event) => { setPriceBasis(event.target.value as PriceBasis); resetAdmission(); }}>
            <option value="raw">原始价（raw）</option>
            <option value="qfq">前复权（qfq）</option>
            <option value="hfq">后复权（hfq）</option>
          </select>
        </label>
      </fieldset>

      <fieldset disabled={busy || loading} hidden={step !== 1}>
        <legend>初始持仓</legend>
        {positions.map((position, index) => (
          <div key={index}>
            <label>
              标的 UUID
              <input value={position.instrument_id} onChange={(event) => { setPositions((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, instrument_id: event.target.value } : item)); resetAdmission(); }} />
            </label>
            <label>
              数量
              <input inputMode="decimal" value={position.quantity} onChange={(event) => { setPositions((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, quantity: event.target.value } : item)); resetAdmission(); }} />
            </label>
            <label>
              可用数量
              <input inputMode="decimal" value={position.available_quantity} onChange={(event) => { setPositions((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, available_quantity: event.target.value } : item)); resetAdmission(); }} />
            </label>
            <label>
              平均成本
              <input inputMode="decimal" value={position.average_price} onChange={(event) => { setPositions((current) => current.map((item, itemIndex) => itemIndex === index ? { ...item, average_price: event.target.value } : item)); resetAdmission(); }} />
            </label>
            <button type="button" onClick={() => { setPositions((current) => current.filter((_, itemIndex) => itemIndex !== index)); resetAdmission(); }}>
              删除持仓
            </button>
          </div>
        ))}
        <button type="button" onClick={() => { setPositions((current) => [...current, emptyPosition()]); resetAdmission(); }}>
          添加初始持仓
        </button>
      </fieldset>

      <fieldset disabled={busy || loading} hidden={step !== 1}>
        <legend>账户与执行</legend>
        <label>
          回测账户
          <select value={accountProfileId} onChange={(event) => { setAccountProfileId(event.target.value); resetAdmission(); }}>
            <option value="">请选择账户</option>
            {accounts.map((account) => (
              <option key={account.id} value={account.id} disabled={account.status !== "active"}>
                {account.name}（v{account.version} · 费用 v{account.fee_schedule_version}）
              </option>
            ))}
          </select>
        </label>
        <label>
          滑点模型
          <select value={slippageIdentity} onChange={(event) => selectSlippage(event.target.value)}>
            <option value="">请选择滑点模型</option>
            {slippageModels.map((model) => (
              <option key={`${model.key}@${model.version}`} value={`${model.key}@${model.version}`}>
                {model.display_name}
              </option>
            ))}
          </select>
        </label>
        {Object.entries(slippageProperties).map(([key, definition]) => (
          <label key={key}>
            {String(definition.title || key)}
            <input
              inputMode="decimal"
              value={String(slippageParameters[key] ?? "")}
              onChange={(event) => {
                setSlippageParameters((current) => ({ ...current, [key]: event.target.value }));
                resetAdmission();
              }}
            />
          </label>
        ))}
        <label>
          随机种子（确定性组件可留空）
          <input type="number" step="1" value={randomSeed} onChange={(event) => { setRandomSeed(event.target.value); resetAdmission(); }} />
        </label>
        <p>所选滑点模型及参数会随本次运行保存。</p>
      </fieldset>

      <fieldset disabled={busy || loading} hidden={step !== 1}><legend>账户版本与分析口径</legend>
        <label>费用方案<select value={feeIdentity} onChange={event => { setFeeIdentity(event.target.value); resetAdmission(); }}>
          <option value="">使用所选账户版本绑定的费用方案</option>
          {feeVersions.map(item => <option key={`${item.key}@${item.version}`} value={`${item.key}@${item.version}`}>{item.display_name} · 版本 {item.version}</option>)}
        </select></label>
        <label>账户历史版本<select value={accountVersion} onChange={(event) => { setAccountVersion(event.target.value); resetAdmission(); }}><option value="">请选择版本</option>{accountVersions.map((item) => <option key={item.version} value={item.version} disabled={item.status !== "active"}>{item.name} · 版本 {item.version} · 费用版本 {item.fee_schedule_version}{item.status !== "active" ? "（不可用）" : ""}</option>)}</select></label>
        <label>夏普口径<select value={sharpeMode} onChange={(event) => { setSharpeMode(event.target.value); resetAdmission(); }}><option value="sharpe_simple">不扣无风险利率</option><option value="sharpe_config_rf">冻结年化无风险利率</option><option value="sharpe_pit_rf">PIT 日利率（当前数据源未提供，仅禁用夏普）</option></select></label>
        {sharpeMode === "sharpe_config_rf" && <><label>年化利率<input value={riskFreeRate} onChange={(event) => { setRiskFreeRate(event.target.value); resetAdmission(); }} /></label><label>利率来源说明<input value={riskFreeNote} onChange={(event) => { setRiskFreeNote(event.target.value); resetAdmission(); }} /></label></>}
        <p>同时计算收益、年化收益、最大回撤、波动率、换手率和费用摘要。</p>
      </fieldset>
      <fieldset disabled={busy || loading} hidden={step !== 1}><legend>系统执行配置</legend>{Object.entries(componentOptions).map(([kind, options]) => <label key={kind}>{({ data_provider: "数据来源", rule_package: "交易规则", calendar_axis_policy: "日历策略", time_axis: "时间轴", timing_policy: "运行时序", execution_model: "成交模型", decision_interpreter: "决策解释", accounting_policy: "账户会计", corporate_action_timing: "公司行动时序" } as Record<string, string>)[kind] || "执行组件"}<select value={`${componentSelections[kind]?.key}@${componentSelections[kind]?.version}`} onChange={(event) => { const item = options.find((option) => `${option.key}@${option.version}` === event.target.value)!; setComponentSelections((current) => ({ ...current, [kind]: { key: item.key, version: item.version, parameters: item.parameters } })); resetAdmission(); }}>{options.map((item) => <option key={`${item.key}@${item.version}`} value={`${item.key}@${item.version}`}>{item.display_name} · v{item.version}</option>)}</select></label>)}</fieldset>

      {step === 2 && <section className="qfb-check">
        <h3>运行配置确认</h3><dl><dt>策略版本</dt><dd>{strategies.find(s => s.id === strategyId)?.name} · 版本 {revisions.find(r => r.id === revisionId)?.revision_number}</dd><dt>回测区间</dt><dd>{startDate} — {endDate}</dd><dt>初始资金</dt><dd>{initialCash} CNY</dd><dt>回测账户</dt><dd>{accounts.find(a => a.id === accountProfileId)?.name || "账户不可用"} · v{accountVersion}</dd></dl>
        <p>检查覆盖数据、策略、账户和执行配置。修改任何配置后都需要重新检查。</p>
        {!preflight && <p>尚未检查当前配置。</p>}
        {preflight && <><h3>{gate?.allowed ? "检查通过" : preflight.status === "degraded" ? "需要确认降级情况" : "检查未通过"}</h3>
          <AdmissionSummary report={preflight} />
          {preflight.status === "degraded" && <label className="qfb-inline"><input type="checkbox" checked={confirmed} disabled={busy} onChange={e => { setConfirmed(e.target.checked); creationKey.current = null; }} />我已阅读降级情况并同意按此配置继续</label>}
          <details><summary>查看完整检查证据</summary><pre>{JSON.stringify(preflight, null, 2)}</pre></details>
        </>}
      </section>}
      {message && <p role="alert" className="qfb-form-message">{message}</p>}
      </div>
      <footer><button type="button" onClick={close} disabled={busy}>取消</button><span />
        {step > 0 && <button type="button" disabled={busy} onClick={() => setStep(step - 1)}>上一步</button>}
        {step < 2 ? <button className="qfb-primary" disabled={busy || loading} onClick={next}>下一步</button> : <>
          <button disabled={busy || loading} onClick={() => void checkAdmission()}>{busy ? "处理中…" : "检查当前配置"}</button>
          <button className="qfb-primary" disabled={busy || !gate?.allowed || (preflight?.status === "degraded" && !confirmed)} onClick={() => void create()}>创建回测</button>
        </>}
      </footer>
      </>}
    </dialog>
  );
}
function AdmissionSummary({ report }: { report: Record<string, any> }) {
  // Keep operator-facing explanations separate from the complete evidence JSON.
  const messages: string[] = [];
  const visit = (value: unknown) => {
    if (Array.isArray(value)) value.forEach(visit);
    else if (value && typeof value === "object") {
      const row = value as Record<string, unknown>;
      if (typeof row.message === "string" && !messages.includes(row.message)) messages.push(row.message);
      Object.values(row).filter(v => typeof v === "object").forEach(visit);
    }
  };
  visit(report.issues);
  visit(report.gates);
  return messages.length ? <ul>{messages.map(message => <li key={message}>{message}</li>)}</ul> : <p>{report.gates?.allowed ? "当前配置满足运行准入要求。" : "当前配置未获得完整准入许可，请展开检查证据。"}</p>;
}
