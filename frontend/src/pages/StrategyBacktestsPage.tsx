import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import { listAccountProfiles, listAccountProfileVersions, type AccountProfile } from "../api/accountProfiles";
import {
  cancelBacktestRun,
  createBacktestRun,
  fetchBacktestResult,
  fetchStrategyBacktestWorkspace,
  FOREGROUND_POLL_INTERVAL_MS,
  getBacktestRun,
  isTerminalBacktestStatus,
  rerunBacktest,
  type BacktestRun,
  type BacktestRunCreateInput,
  type ComponentDescriptor,
  type ComponentSelectionInput,
  BacktestApiError,
} from "../api/backtestRuns";
import {
  compareBacktestRuns,
  preflightBacktest,
} from "../api/backtestPreflight";
import { admissionAllowsCreation } from "../components/backtestAdmission";
import { BacktestReport, BacktestComparisonView } from "../components/BacktestReport";
import { getStrategy, type StrategyDetail } from "../api/strategies";

const labels: Record<string, string> = {
  queued: "排队中",
  starting: "启动中",
  running: "运行中",
  cancel_requested: "取消处理中",
  succeeded: "已成功",
  failed: "失败",
  cancelled: "已取消",
  timed_out: "已超时",
  indeterminate: "结果待判定",
};
const tabs = ["steps", "decisions", "orders", "fills", "positions", "equity", "metrics"] as const;
const today = new Date().toISOString().slice(0, 10);

type Tab = (typeof tabs)[number];
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

const stale = (run: BacktestRun) =>
  run.last_heartbeat_at &&
  !isTerminalBacktestStatus(run.status) &&
  Date.now() - Date.parse(run.last_heartbeat_at) > 60_000;

const show = (value: unknown) =>
  value == null ? "—" : typeof value === "string" ? value : JSON.stringify(value);

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

export function StrategyBacktestsPage() {
  const { strategyId = "" } = useParams<{ strategyId: string }>();
  const [strategy, setStrategy] = useState<StrategyDetail | null>(null);
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
  const [accountVersion, setAccountVersion] = useState("");
  const [sharpeMode, setSharpeMode] = useState("sharpe_simple");
  const [riskFreeRate, setRiskFreeRate] = useState("0");
  const [riskFreeNote, setRiskFreeNote] = useState("");
  const [selectedRunIds, setSelectedRunIds] = useState<string[]>([]);
  const [runCursor, setRunCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inputGeneration = useRef(0);
  const admissionFingerprint = useRef<string | null>(null);
  const creationKey = useRef<string | null>(null);
  const [runs, setRuns] = useState<BacktestRun[]>([]);
  const [selected, setSelected] = useState<BacktestRun | null>(null);
  const [message, setMessage] = useState("");
  const [preflight, setPreflight] = useState<Record<string, any> | null>(null);
  const [gate, setGate] = useState<Record<string, any> | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [compareResult, setCompareResult] = useState<any>(null);
  const [tab, setTab] = useState<Tab>("metrics");
  const [pages, setPages] = useState<Record<string, any[]>>({});
  const [cursors, setCursors] = useState<Record<string, string | null>>({});
  const selectedRef = useRef<BacktestRun | null>(null);
  const workspaceGeneration = useRef(0);
  const runsRef = useRef(runs);
  runsRef.current = runs;
  const detailGeneration = useRef(0);

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

  const load = async (generation: number) => {
    try {
      const [strategyDetail, workspace, availableAccounts] = await Promise.all([
        getStrategy(strategyId),
        fetchStrategyBacktestWorkspace(strategyId),
        listAccountProfiles(),
      ]);
      if (generation !== workspaceGeneration.current) return;
      const published = (workspace.published_revisions || []) as PublishedRevision[];
      const latest = [...published].sort(
        (left, right) => right.revision_number - left.revision_number,
      )[0];
      const availableSlippage = workspace.slippage_models || [];
      const noSlippage =
        availableSlippage.find((item) => item.key === "none" && item.version === 1) ||
        availableSlippage[0];

      setStrategy(strategyDetail);
      setRevisions(published);
      // Historical run evidence must not replace admission for current inputs.
      setRuns(
        (workspace.runs?.items || []).filter(
          (run: BacktestRun) => run.run_kind === "backtest_run",
        ),
      );
      setRunCursor(workspace.runs.next_cursor || null);
      setComponentOptions(workspace.component_options || {});
      setComponentSelections(Object.fromEntries(Object.entries(workspace.component_options || {}).filter(([, options]) => options.length > 0).map(([kind, options]) => [kind, { key: options[0].key, version: options[0].version, parameters: options[0].parameters }])));
      setAccounts(availableAccounts);
      setAccountProfileId((current) => current || availableAccounts[0]?.id || "");
      setSlippageModels(availableSlippage);
      if (latest) selectRevision(latest.id, published);
      if (noSlippage) {
        selectSlippage(`${noSlippage.key}@${noSlippage.version}`, availableSlippage);
      }
    } catch (error) {
      if (generation !== workspaceGeneration.current) return;
      setMessage(error instanceof Error ? error.message : "工作台加载失败。");
    }
  };

  useEffect(() => {
    const generation = ++workspaceGeneration.current;
    resetAdmission(); setStrategy(null); setRevisions([]); setRevisionId("");
    setRuns([]); setSelected(null); selectedRef.current = null;
    detailGeneration.current += 1;
    setSelectedRunIds([]); setCompareResult(null); setRunCursor(null);
    setPages({}); setCursors({}); setMessage(""); setBusy(false);
    void load(generation);
    return () => { workspaceGeneration.current += 1; inputGeneration.current += 1; };
  }, [strategyId]);

  useEffect(() => {
    let active = true;
    setAccountVersions([]); setAccountVersion(""); resetAdmission();
    if (accountProfileId) listAccountProfileVersions(accountProfileId).then((versions) => {
      if (active) { setAccountVersions(versions); setAccountVersion(String(versions.find((item) => item.status === "active")?.version || "")); }
    }).catch((error: Error) => { if (active) setMessage(error.message); });
    return () => { active = false; };
  }, [accountProfileId]);

  const hasActiveRun = runs.some((run) => !isTerminalBacktestStatus(run.status));
  useEffect(() => {
    if (!hasActiveRun) return;
    let stopped = false;
    let timer: number | undefined;

    const poll = async () => {
      if (stopped || document.visibilityState !== "visible") return;
      try {
        const workspace = await fetchStrategyBacktestWorkspace(strategyId);
        // Historical run evidence must not replace admission for current inputs.
        const nextRuns = (workspace.runs?.items || []).filter(
          (run: BacktestRun) => run.run_kind === "backtest_run",
        );
        // Runs loaded from older pages remain live even when absent from the
        // newest page. Refresh those identities through the run endpoint.
        const olderActive = runsRef.current.filter((item) => !isTerminalBacktestStatus(item.status) && !nextRuns.some((fresh) => fresh.run_id === item.run_id));
        nextRuns.push(...await Promise.all(olderActive.map((item) => getBacktestRun(item.run_id))));
        if (stopped) return;
        setRuns((current) => [...nextRuns, ...current.filter((item) => !nextRuns.some((fresh: BacktestRun) => fresh.run_id === item.run_id))]);
        if (selectedRef.current) {
          const fresh = nextRuns.find(
            (run: BacktestRun) => run.run_id === selectedRef.current?.run_id,
          );
          if (fresh) {
            selectedRef.current = fresh;
            setSelected(fresh);
          }
        }
      } catch (error) {
        if (!stopped) setMessage(error instanceof Error ? error.message : "运行状态刷新失败。");
      } finally {
        // A transient request failure must not permanently stop a live run's
        // foreground polling cycle.
        if (!stopped && document.visibilityState === "visible") {
          timer = window.setTimeout(poll, FOREGROUND_POLL_INTERVAL_MS);
        }
      }
    };
    const visibilityChanged = () => {
      if (timer) window.clearTimeout(timer);
      if (document.visibilityState === "visible") void poll();
    };

    void poll();
    document.addEventListener("visibilitychange", visibilityChanged);
    return () => {
      stopped = true;
      if (timer) window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", visibilityChanged);
    };
  }, [hasActiveRun, strategyId]);

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
    if (!accountProfileId || !accountVersion) {
      setMessage("请选择可用的回测账户版本。");
      return null;
    }
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

  const fingerprint = (payload: BacktestRunCreateInput) => JSON.stringify({ ...payload, data_cutoff: undefined, idempotency_key: undefined, degraded: undefined, confirmed_admission_report_hash: undefined });

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
      setRuns((current) => [run, ...current.filter((item) => item.run_id !== run.run_id)]);
      if (generation === inputGeneration.current) { creationKey.current = null; setMessage("正式回测已加入队列。"); }
    } catch (error) {
      if (generation === inputGeneration.current) setMessage(error instanceof Error ? error.message : "创建回测失败。");
    } finally { if (pageGeneration === workspaceGeneration.current) setBusy(false); }
  };

  const loadMoreRuns = async () => {
    if (!runCursor) return;
    const generation = workspaceGeneration.current;
    try {
      const workspace = await fetchStrategyBacktestWorkspace(strategyId, undefined, runCursor);
      if (generation !== workspaceGeneration.current) return;
      setRuns((current) => [...current, ...workspace.runs.items.filter((run) => !current.some((item) => item.run_id === run.run_id))]);
      setRunCursor(workspace.runs.next_cursor || null);
    } catch (error) { if (generation === workspaceGeneration.current) setMessage(error instanceof Error ? error.message : "运行列表加载失败。"); }
  };

  const loadTab = async (runId: string, kind: string, cursor?: string) => {
    const generation = detailGeneration.current;
    let page;
    try { page = await fetchBacktestResult(runId, kind, cursor); }
    catch (error) { if (generation === detailGeneration.current) setMessage(error instanceof Error ? error.message : "结果加载失败。"); return; }
    if (selectedRef.current?.run_id !== runId || generation !== detailGeneration.current) return;
    setPages((current) => ({
      ...current,
      [kind]: cursor
        ? [...(current[kind] || []), ...page.items]
        : page.items,
    }));
    setCursors((current) => ({
      ...current,
      [kind]: page.next_cursor || null,
    }));
  };

  const open = async (run: BacktestRun) => {
    const generation = ++detailGeneration.current;
    selectedRef.current = run;
    setSelected(run);
    setPages({});
    setCursors({});
    try {
      const detail = await getBacktestRun(run.run_id);
      if (selectedRef.current?.run_id !== detail.run_id || generation !== detailGeneration.current) return;
      setSelected(detail);
      selectedRef.current = detail;
      await loadTab(detail.run_id, tab);
    } catch (error) {
      if (generation === detailGeneration.current) setMessage(error instanceof Error ? error.message : "详情加载失败。");
    }
  };

  const compare = async () => {
    const generation = workspaceGeneration.current;
    const ids = selectedRunIds;
    if (ids.length < 2) {
      setMessage("请选择两个已完成的正式运行进行比较。");
      return;
    }
    try {
      const result = await compareBacktestRuns(ids);
      if (generation !== workspaceGeneration.current) return;
      setCompareResult(result);
      setMessage("比较结果已加载。");
    } catch (error: any) {
      if (generation !== workspaceGeneration.current) return;
      setMessage(
        error instanceof BacktestApiError
          ? `${error.message}（${error.code || `HTTP ${error.status}`}）`
          : "比较失败。",
      );
    }
  };

  const updateRun = async (run: BacktestRun, action: "rerun" | "cancel") => {
    const generation = workspaceGeneration.current;
    try {
      const fresh = (action === "rerun" ? await rerunBacktest(run) : await cancelBacktestRun(run.run_id)) as BacktestRun;
      if (generation !== workspaceGeneration.current) return;
      // Updating history must leave the current form and its admission alone.
      setRuns((current) => [fresh, ...current.filter((item) => item.run_id !== fresh.run_id)]);
      if (selectedRef.current?.run_id === fresh.run_id) { selectedRef.current = fresh; setSelected(fresh); }
    } catch (error) {
      if (generation === workspaceGeneration.current) setMessage(error instanceof Error ? error.message : "运行操作失败。");
    }
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
    <section className="strategy-backtests" data-polling-protocol="foreground_polling@1">
      <h1>{strategy?.name || "策略"} · 回测工作台</h1>
      <p>创建时冻结以下输入；后续草稿、账户或页面修改不会改变已创建运行。</p>

      <fieldset>
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

      <fieldset>
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
          Warmup 交易会话数
          <input type="number" min="0" max="512" value={warmupSessions} onChange={(event) => { setWarmupSessions(event.target.value); resetAdmission(); }} />
        </label>
        <p>当前数据源支持固定标的范围；动态标的范围尚未开放。</p>
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

      <fieldset>
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
        <button type="button" onClick={() => setPositions((current) => [...current, emptyPosition()])}>
          添加初始持仓
        </button>
      </fieldset>

      <fieldset>
        <legend>账户与执行</legend>
        <label>
          回测账户
          <select value={accountProfileId} onChange={(event) => { setAccountProfileId(event.target.value); resetAdmission(); }}>
            <option value="">请选择账户</option>
            {accounts.map((account) => (
              <option key={account.id} value={account.id}>
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
        <p>零滑点会明确冻结为 <code>none@1</code>，不会以空值代替。</p>
      </fieldset>

      <fieldset><legend>账户版本与分析口径</legend>
        <label>账户历史版本<select value={accountVersion} onChange={(event) => { setAccountVersion(event.target.value); resetAdmission(); }}><option value="">请选择版本</option>{accountVersions.map((item) => <option key={item.version} value={item.version} disabled={item.status !== "active"}>{item.name} · 版本 {item.version} · 费用版本 {item.fee_schedule_version}{item.status !== "active" ? "（不可用）" : ""}</option>)}</select></label>
        <label>夏普口径<select value={sharpeMode} onChange={(event) => { setSharpeMode(event.target.value); resetAdmission(); }}><option value="sharpe_simple">不扣无风险利率</option><option value="sharpe_config_rf">冻结年化无风险利率</option><option value="sharpe_pit_rf">PIT 日利率（当前数据源未提供，仅禁用夏普）</option></select></label>
        {sharpeMode === "sharpe_config_rf" && <><label>年化利率<input value={riskFreeRate} onChange={(event) => { setRiskFreeRate(event.target.value); resetAdmission(); }} /></label><label>利率来源说明<input value={riskFreeNote} onChange={(event) => { setRiskFreeNote(event.target.value); resetAdmission(); }} /></label></>}
        <p>同时计算收益、年化收益、最大回撤、波动率、换手率和费用摘要。</p>
      </fieldset>
      <fieldset><legend>系统执行配置</legend>{Object.entries(componentOptions).map(([kind, options]) => <label key={kind}>{({ data_provider: "数据来源", rule_package: "交易规则", calendar_axis_policy: "日历策略", time_axis: "时间轴", timing_policy: "运行时序", execution_model: "成交模型", decision_interpreter: "决策解释", accounting_policy: "账户会计", corporate_action_timing: "公司行动时序" } as Record<string, string>)[kind] || "执行组件"}<select value={`${componentSelections[kind]?.key}@${componentSelections[kind]?.version}`} onChange={(event) => { const item = options.find((option) => `${option.key}@${option.version}` === event.target.value)!; setComponentSelections((current) => ({ ...current, [kind]: { key: item.key, version: item.version, parameters: item.parameters } })); resetAdmission(); }}>{options.map((item) => <option key={`${item.key}@${item.version}`} value={`${item.key}@${item.version}`}>{item.display_name} · v{item.version}</option>)}</select></label>)}</fieldset>
      {gate && (
        <details>
          <summary>正式准入门禁：{String(gate.status || "未知")}</summary>
          <pre>{JSON.stringify(gate, null, 2)}</pre>
        </details>
      )}
      {preflight && (
        <label>
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(event) => setConfirmed(event.target.checked)}
            disabled={preflight.status !== "degraded"}
          />
          确认降级报告 hash 后继续
        </label>
      )}
      <button type="button" disabled={busy || !revisionId || !accountProfileId || !accountVersion} onClick={() => void checkAdmission()}>预检当前配置</button>{" "}
      <button type="button" disabled={busy || !gate?.allowed || (preflight?.status === "degraded" && !confirmed)} onClick={() => void create()}>创建正式回测</button>{" "}
      <button type="button" disabled={selectedRunIds.length < 2} onClick={() => void compare()}>
        比较运行
      </button>
      {message && <p role="status">{message}</p>}
      {preflight && (
        <details open>
          <summary>准入报告：{String(preflight.status || "未知")}</summary>
          <pre>{JSON.stringify(preflight, null, 2)}</pre>
        </details>
      )}

      <div>
        {runs.map((run) => (
          <article key={run.run_id}>
            <label><input type="checkbox" aria-label={`比较 ${run.run_id}`} checked={selectedRunIds.includes(run.run_id)} disabled={run.status !== "succeeded" || (!selectedRunIds.includes(run.run_id) && selectedRunIds.length >= 10)} onChange={(event) => setSelectedRunIds((current) => event.target.checked ? [...current, run.run_id] : current.filter((id) => id !== run.run_id))} />加入比较</label>
            <button type="button" onClick={() => void open(run)}>
              {run.run_id} · {labels[run.status] || "运行状态（待识别）"}
            </button>
            <span>
              日期 {run.current_trading_date || "—"} · 步骤 {run.current_step || "—"} · 进度{" "}
              {Math.round((run.progress_ratio || 0) * 100)}% · 心跳 {run.last_heartbeat_at || "—"}
            </span>
            {stale(run) && <p role="alert">心跳已超过 60 秒未更新，请关注运行状态（不会修改状态）。</p>}
            {isTerminalBacktestStatus(run.status) && (
              <button type="button" onClick={() => void updateRun(run, "rerun")}>
                重新运行
              </button>
            )}
            {["queued", "starting", "running", "cancel_requested"].includes(run.status) && (
              <button type="button" onClick={() => void updateRun(run, "cancel")}>
                取消
              </button>
            )}
          </article>
        ))}
      </div>

      {runCursor && <button type="button" onClick={() => void loadMoreRuns()}>加载更多运行</button>}

      {selected && (
        <aside aria-label="回测详情">
          <h2>运行详情</h2>
          <BacktestReport run={selected} />
          <p>
            状态：{labels[selected.status] || "未知"}；冻结 binding：
            {show((selected as any).resolved_run_binding || selected.strategy_revision_id)}；终态证据：
            {show(selected.terminal_decision_reason)}
          </p>
          <p>
            失败定位：phase={show(selected.failure_evidence?.failure_phase || selected.failure_phase)} type=
            {show(selected.failure_evidence?.error_type || selected.failure_type)} step=
            {show(selected.failure_evidence?.failure_step ?? selected.failure_step)} line=
            {show(selected.failure_evidence?.source_line ?? selected.source_line)}
          </p>
          {selected.failure_evidence && (
            <details open>
              <summary>脱敏失败详情</summary>
              <p>{show(selected.failure_evidence.message || selected.error_message)}</p>
              <pre>{String(selected.failure_evidence.technical_detail || selected.technical_detail || "暂无技术详情")}</pre>
            </details>
          )}
          <p>结果在运行完成前标记为未完成；敏感凭据和无限 stdout 不展示。</p>
          {tabs.map((item) => (
            <button key={item} type="button" onClick={() => { setTab(item); void loadTab(selected.run_id, item); }}>
              {item}
            </button>
          ))}
          <pre>{JSON.stringify(pages[tab] || [], null, 2)}</pre>
          {cursors[tab] && (
            <button type="button" onClick={() => void loadTab(selected.run_id, tab, cursors[tab] || undefined)}>
              加载更多结果
            </button>
          )}
        </aside>
      )}

      {compareResult != null && (
        <section aria-label="运行比较结果">
          <h2>比较结果</h2>
          <BacktestComparisonView result={compareResult} />
        </section>
      )}
    </section>
  );
}
