import type { BacktestRun, BacktestRunCreateInput } from "../../api/backtestRuns";
export const STATUS: Record<string, string> = { queued: "排队中", starting: "启动中", running: "运行中", cancel_requested: "取消处理中", succeeded: "已完成", failed: "失败", cancelled: "已取消", timed_out: "已超时", indeterminate: "结果待判定" };
export const METRICS = [{ key: "total_return", name: "总收益", percent: true }, { key: "annualized_return", name: "年化收益", percent: true }, { key: "max_drawdown", name: "最大回撤", percent: true }, { key: "sharpe", name: "夏普比率", percent: false }, { key: "volatility", name: "年化波动率", percent: true }, { key: "turnover", name: "换手率", percent: false }];
export function finite(value: unknown): number | null { return value === null || value === undefined || value === "" || !Number.isFinite(Number(value)) ? null : Number(value); }
export function metricText(value: unknown, percent: boolean): string { const number = finite(value); return number === null ? "—" : percent ? `${(number * 100).toFixed(2)}%` : number.toLocaleString("zh-CN", { maximumFractionDigits: 4 }); }
export function runConfig(run: BacktestRun): Record<string, any> { return (run.backtest_config?.spec || run.backtest_config || {}) as Record<string, any>; }
/** Only editable inputs participate in admission identity; transport evidence is renewed. */
export function configurationFingerprint(payload: BacktestRunCreateInput): string {
  return JSON.stringify({ ...payload, data_cutoff: undefined, idempotency_key: undefined, degraded: undefined, confirmed_admission_report_hash: undefined });
}
/** Copy the frozen input whitelist, never the old gate, cutoff, or request identity.
 * Unsupported historical shapes are rejected explicitly rather than silently
 * changing financial assumptions to today's form defaults.
 */
export function copyConfiguration(run: BacktestRun): BacktestRunCreateInput {
  const spec = runConfig(run);
  if (!spec.slippage_model || !spec.component_selections || !spec.analyzer_selections || !spec.start_date || !spec.end_date || !spec.account_profile_version) throw new Error("此记录缺少完整配置，无法复制；请新建回测或使用重新运行。");
  if (spec.dynamic_universe || spec.currency !== "CNY" || spec.timezone !== "Asia/Shanghai" || spec.frequency !== "1d" || spec.strategy_price_bases?.length !== 1 || spec.initial_positions?.some((p: any) => p.side !== "long")) throw new Error("此历史配置包含当前创建表单不支持的选项，请使用重新运行保留原配置。");
  const analyzers = spec.analyzer_selections as { key: string; version: number }[];
  if (analyzers.length !== 4 || analyzers.some(a => a.version !== 1 || !["performance", "turnover", "fee_summary", "sharpe_simple", "sharpe_config_rf", "sharpe_pit_rf"].includes(a.key)) || analyzers.filter(a => a.key.startsWith("sharpe_")).length !== 1) throw new Error("此历史分析器组合无法通过当前表单完整复制，请使用重新运行。");
  if (analyzers.some((a: any) => Object.keys(a.parameters || {}).some(key => a.key !== "sharpe_config_rf" || !["rf_annual", "rf_source_note"].includes(key)))) throw new Error("此历史分析器参数无法完整复制，请使用重新运行。");
  return structuredClone({
    strategy_revision_id: run.strategy_revision_id || spec.strategy_revision_id,
    parameters: run.parameters || spec.strategy_parameters || {},
    backtest_config: { start_date: spec.start_date, end_date: spec.end_date, initial_cash: spec.initial_cash, initial_positions: spec.initial_positions || [], dynamic_universe: false, instrument_ids: spec.instrument_ids || [], exchanges: spec.exchanges, strategy_price_bases: spec.strategy_price_bases, currency: spec.currency, timezone: spec.timezone, frequency: spec.frequency, warmup_sessions: spec.warmup_sessions },
    account_profile_id: spec.account_profile_id, account_profile_version: spec.account_profile_version,
    fee_schedule_selection: spec.fee_schedule_selection || undefined, component_selections: spec.component_selections,
    analyzer_selections: spec.analyzer_selections, slippage_model: spec.slippage_model, random_seed: spec.random_seed,
  });
}
