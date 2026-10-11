import type { Capabilities, RunConfig } from "../../../api/runService";

export const STATUS: Record<string, string> = { queued: "排队中", starting: "启动中", running: "运行中", cancel_requested: "取消处理中", succeeded: "已完成", failed: "失败", cancelled: "已取消", timed_out: "超时", indeterminate: "结果待核对" };
export const terminal = (status: string) => ["succeeded", "failed", "cancelled", "timed_out", "indeterminate"].includes(status);
export const modelName: Record<string, string> = { bar_next_interval_v1: "下一 Bar 区间（收盘确认成交）", trade_tick_v1: "成交 Tick（共享成交量）", quote_tick_v1: "报价 Tick（双边保守额度）" };
const decimal = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/;
const unsignedDecimal = /^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/;
export function compareDecimal(a: string, b: string): number {
  if (![a, b].every(v => typeof v === "string" && v.length <= 128 && decimal.test(v))) throw new Error("金额与比例须为有限的十进制字符串。");
  const scale = Math.max(a.split(".")[1]?.length || 0, b.split(".")[1]?.length || 0);
  const integer = (v: string) => { const [head, fraction = ""] = v.split("."); return BigInt(head.replace("-", "") + fraction.padEnd(scale, "0")) * (v.startsWith("-") ? -1n : 1n); };
  const x = integer(a), y = integer(b); return x < y ? -1 : x > y ? 1 : 0;
}
export function jsonObject(source: string): Record<string, unknown> {
  // Inspect unquoted number tokens before JSON.parse can round unsafe integers.
  for (const token of source.match(/"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g) || []) {
    if (token.startsWith('"')) continue;
    const n = Number(token); if (!Number.isFinite(n) || Number.isInteger(n) && !Number.isSafeInteger(n)) throw new Error("参数中的大整数须用字符串保存，且数值必须有限。");
  }
  const value = JSON.parse(source);
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("每项策略参数须为 JSON 对象。");
  return value;
}
export function validateConfig(config: RunConfig, caps: Capabilities): void {
  if (config.api_schema !== "qf.backtest.v2" || config.currency !== undefined && config.currency !== "CNY") throw new Error("新运行须使用 qf.backtest.v2 与 CNY。");
  if (!config.strategy_revision_id || !config.account_id) throw new Error("请选择已发布策略修订与可用账户。");
  for (const date of [config.start, config.end]) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || Number.isNaN(Date.parse(date)) || new Date(date).toISOString().slice(0, 10) !== date) throw new Error("请选择有效的起止日期。");
  }
  if (config.start > config.end) throw new Error("开始日期不能晚于结束日期。");
  if (!config.universe.length || config.universe.length > 10000 || config.universe.some(v => typeof v !== "string" || !v) || new Set(config.universe).size !== config.universe.length) throw new Error("标的集合须包含 1–10000 个不重复的代码。");
  if (compareDecimal(config.initial_cash, "0") <= 0) throw new Error("初始资金必须大于 0，账户选择不会自动提供资本。");
  if (compareDecimal(config.participation_rate || "", "0") <= 0 || compareDecimal(config.participation_rate!, "1") > 0) throw new Error("模型参与率必须大于 0 且不超过 1。");
  if (!unsignedDecimal.test(config.slippage_bps || "") || compareDecimal(config.slippage_bps!, "10000") > 0) throw new Error("滑点须为 0–10000 基点。");
  if (compareDecimal(config.risk_free_rate || "", "-1") <= 0) throw new Error("年化无风险利率须大于 -1。");
  if (!Number.isInteger(config.seed) || config.seed! < 0 || config.seed! > 2147483647) throw new Error("随机种子须为 0–2147483647 的整数。");
  if (!Number.isInteger(config.annualization_sessions) || config.annualization_sessions! < 1 || config.annualization_sessions! > 366) throw new Error("年化会话数须为 1–366 的整数。");
  if (!caps.engine.installed || !caps.engine.frequencies.includes(config.frequency) || !caps.engine.execution_models.includes(config.execution_model)) throw new Error("服务未提供所选频率或模型的引擎能力。");
  if (!["session_close", "bar_close"].includes(config.result_sampling || "")) throw new Error("请选择服务支持的结果采样方式。");
  if (config.frequency === "tick" ? config.execution_model === "bar_next_interval_v1" || config.result_sampling !== "session_close" : config.execution_model !== "bar_next_interval_v1") throw new Error("频率、撮合模型与结果采样不匹配。");
  if (config.cost_overrides?.commission_rate !== undefined && (!unsignedDecimal.test(config.cost_overrides.commission_rate) || compareDecimal(config.cost_overrides.commission_rate, "1") > 0)) throw new Error("佣金率须在 0–1 之间。");
  if (config.cost_overrides?.minimum_commission !== undefined && (!unsignedDecimal.test(config.cost_overrides.minimum_commission) || compareDecimal(config.cost_overrides.minimum_commission, "0") < 0)) throw new Error("最低佣金须非负。");
  if (Object.keys(config.parameters).length > 128 || new TextEncoder().encode(JSON.stringify(config.parameters)).length > caps.limits.parameter_bytes) throw new Error("策略参数超过服务公布的大小预算。");
  const depth = (v: unknown, d = 0): number => v && typeof v === "object" ? Math.max(d, ...Object.values(v).map(x => depth(x, d + 1))) : d;
  if (depth(config.parameters) > caps.limits.parameter_depth) throw new Error("策略参数嵌套超过服务公布的深度预算。");
}
