import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import { listAccountProfiles, type AccountProfile } from "../api/accountProfiles";
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
  BacktestApiError,
} from "../api/backtestRuns";
import {
  compareBacktestRuns,
  fetchBacktestPreflight,
  preflightBacktest,
} from "../api/backtestPreflight";
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
  const [dynamicUniverse, setDynamicUniverse] = useState(true);
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

  const resetAdmission = () => {
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

  const load = async () => {
    try {
      const [strategyDetail, workspace, availableAccounts] = await Promise.all([
        getStrategy(strategyId),
        fetchStrategyBacktestWorkspace(strategyId),
        listAccountProfiles(),
      ]);
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
      setGate(workspace.formal_gate || null);
      setRuns(
        (workspace.runs?.items || []).filter(
          (run: BacktestRun) => run.run_kind === "backtest_run",
        ),
      );
      setAccounts(availableAccounts);
      setAccountProfileId((current) => current || availableAccounts[0]?.id || "");
      setSlippageModels(availableSlippage);
      if (latest) selectRevision(latest.id, published);
      if (noSlippage) {
        selectSlippage(`${noSlippage.key}@${noSlippage.version}`, availableSlippage);
      }
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "工作台加载失败。");
    }
  };

  useEffect(() => {
    void load();
  }, [strategyId]);

  const hasActiveRun = runs.some((run) => !isTerminalBacktestStatus(run.status));
  useEffect(() => {
    if (!hasActiveRun) return;
    let stopped = false;
    let timer: number | undefined;

    const poll = async () => {
      if (stopped || document.visibilityState !== "visible") return;
      try {
        const workspace = await fetchStrategyBacktestWorkspace(strategyId);
        setGate(workspace.formal_gate || null);
        const nextRuns = (workspace.runs?.items || []).filter(
          (run: BacktestRun) => run.run_kind === "backtest_run",
        );
        setRuns(nextRuns);
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
        setMessage(error instanceof Error ? error.message : "运行状态刷新失败。");
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
    if (!accountProfileId) {
      setMessage("请选择回测账户。");
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

  const create = async () => {
    const key = crypto.randomUUID();
    const payload = buildPayload(key);
    if (!payload) return;
    try {
      const admission = (await preflightBacktest(payload)) as Record<string, any>;
      setPreflight(admission);
      setGate({
        status: admission.status,
        allowed: admission.status !== "blocked",
        gates: admission.gates || {},
        report_hash: admission.report_hash || null,
      });
      if (
        admission.status === "blocked" ||
        admission.code?.includes("blocked") ||
        admission.account_snapshot_available === false ||
        admission.fee_snapshot_available === false ||
        admission.holdings_eligible === false
      ) {
        setMessage(
          admission.message ||
            "账户、费用快照不可用或初始持仓缺少资格，已阻断正式回测。",
        );
        return;
      }
      if (admission.status === "degraded" && !confirmed) {
        setMessage("预检结果为降级，请确认同一报告后再创建。");
        return;
      }
      const run = (await createBacktestRun(payload, key)) as BacktestRun;
      setRuns((current) => [run, ...current.filter((item) => item.run_id !== run.run_id)]);
      setGate(run.formal_gates || gate);
      setMessage("正式回测已加入队列。");
    } catch (error: any) {
      setMessage(
        error instanceof BacktestApiError
          ? `${error.message}（${error.code || `HTTP ${error.status}`}）`
          : error?.message || "创建回测失败。",
      );
    }
  };

  const loadTab = async (runId: string, kind: string, cursor?: string) => {
    const page = await fetchBacktestResult(runId, kind, cursor);
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
    selectedRef.current = run;
    setSelected(run);
    setPages({});
    setCursors({});
    try {
      const detail = await getBacktestRun(run.run_id);
      setSelected(detail);
      selectedRef.current = detail;
      setPreflight((await fetchBacktestPreflight(detail.run_id)) as any);
      setGate(detail.formal_gates || gate);
      await loadTab(detail.run_id, tab);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "详情加载失败。");
    }
  };

  const compare = async () => {
    const ids = runs
      .filter((run) => run.status === "succeeded")
      .slice(0, 2)
      .map((run) => run.run_id);
    if (ids.length < 2) {
      setMessage("请选择两个已完成的正式运行进行比较。");
      return;
    }
    try {
      setCompareResult(await compareBacktestRuns(ids));
      setMessage("比较结果已加载。");
    } catch (error: any) {
      setMessage(
        error instanceof BacktestApiError
          ? `${error.message}（${error.code || `HTTP ${error.status}`}）`
          : "比较失败。",
      );
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
        <label>
          <input type="checkbox" checked={dynamicUniverse} onChange={(event) => { setDynamicUniverse(event.target.checked); resetAdmission(); }} />
          启用动态候选范围
        </label>
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
      <button type="button" disabled={!revisionId || !accountProfileId} onClick={() => void create()}>
        预检并创建正式回测
      </button>{" "}
      <button type="button" onClick={() => void compare()}>
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
            <button type="button" onClick={() => void open(run)}>
              {run.run_id} · {labels[run.status] || "运行状态（待识别）"}
            </button>
            <span>
              日期 {run.current_trading_date || "—"} · 步骤 {run.current_step || "—"} · 进度{" "}
              {Math.round((run.progress_ratio || 0) * 100)}% · 心跳 {run.last_heartbeat_at || "—"}
            </span>
            {stale(run) && <p role="alert">心跳已超过 60 秒未更新，请关注运行状态（不会修改状态）。</p>}
            {isTerminalBacktestStatus(run.status) && (
              <button type="button" onClick={() => void rerunBacktest(run).then(load)}>
                重新运行
              </button>
            )}
            {["queued", "starting", "running", "cancel_requested"].includes(run.status) && (
              <button type="button" onClick={() => void cancelBacktestRun(run.run_id).then(load)}>
                取消
              </button>
            )}
          </article>
        ))}
      </div>

      {selected && (
        <aside aria-label="回测详情">
          <h2>运行详情</h2>
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
          <details>
            <summary>数据准入预检（canonical）</summary>
            <pre>{JSON.stringify(preflight, null, 2)}</pre>
          </details>
        </aside>
      )}

      {compareResult != null && (
        <section aria-label="运行比较结果">
          <h2>比较结果</h2>
          <Comparison result={compareResult} />
        </section>
      )}
    </section>
  );
}

function Curve({ title, series }: { title: string; series: unknown }) {
  const points = Array.isArray(series)
    ? series.filter((point: any) => Array.isArray(point) && point.length >= 2)
    : [];
  const xy = points
    .map(
      (point: any, index: number) =>
        `${(index / Math.max(points.length - 1, 1)) * 100},${100 - (Number(point[1]) || 0)}`,
    )
    .join(" ");
  return (
    <section>
      <h3>{title}</h3>
      <svg
        role="img"
        aria-label={title}
        viewBox="0 0 100 100"
        preserveAspectRatio="none"
        style={{ width: "100%", height: 160, border: "1px solid currentColor" }}
      >
        <polyline fill="none" stroke="currentColor" points={xy} />
      </svg>
      <pre>{JSON.stringify(series ?? "无数据", null, 2)}</pre>
    </section>
  );
}

function Comparison({ result }: { result: any }) {
  const section = (title: string, value: unknown) => (
    <section>
      <h3>{title}</h3>
      <pre>{JSON.stringify(value ?? "无数据", null, 2)}</pre>
    </section>
  );
  return (
    <div>
      <Curve title="净值曲线（持久化点）" series={result.equity_curve_series} />
      <Curve title="回撤曲线（持久化点）" series={result.drawdown_curve_series} />
      {section("指标矩阵/公式版本/不可用原因", result.metric_matrix)}
      {section("配置差异（参数、data request、behavior versions）", result.configuration_diff)}
    </div>
  );
}
