import type { AccountProfile, AccountProfilePayload, AccountProfileStatus, FeeRule } from "../../api/accountProfiles";

export const STATUS: Record<AccountProfileStatus, string> = { active: "可选择", inactive: "停用", retired: "已退役" };
export const CATEGORIES: Record<string, string> = { commission: "佣金", stamp_tax: "印花税", transfer_fee: "过户费", handling_fee: "经手费", regulatory_fee: "监管费", other: "其他费用" };
export const LEVELS = { fee_item: "费用项", fill: "成交", order: "订单" };
export const MODES = { half_up: "四舍五入", up: "向上取整", down: "向下取整" };
export const META: Record<string, string> = { currency: "币种", account_type: "账户类型", broker: "券商 / 交易席位", owner: "账户拥有者" };
export type Pair = { key: string; value: string };
export type EditorRule = FeeRule & { rows: Pair[] };
export type Draft = { name: string; status: AccountProfileStatus; scheduleKey: string; rules: EditorRule[]; metadata: Pair[] };
export type Issue = { step: number; field: string; message: string; rule?: number };
export const pairs = (value: Record<string, string>): Pair[] => Object.entries(value).map(([key, value]) => ({ key, value }));
export function newRule(key = "commission"): EditorRule {
  // Visible starting values are editable; missing historical fields are never
  // filled with these defaults when opening an existing configuration.
  return { key, category: "commission", side: null, rate: "0.0003", minimum: "5", fixed_amount: "0", rounding_level: "fee_item", rounding_scope: key, rounding_mode: "half_up", rounding_precision: "0.01", applicability: {}, rows: [] };
}
export function makeDraft(profile?: AccountProfile, copy = false): Draft {
  const metadata = profile ? Object.fromEntries(Object.entries(profile.metadata).filter((entry): entry is [string, string] => typeof entry[1] === "string")) : { currency: "CNY", account_type: "cash" };
  return { name: profile ? profile.name + (copy ? " 副本" : "") : "", status: profile?.status ?? "active", scheduleKey: profile?.fee_schedule.key ?? "", metadata: pairs(metadata), rules: profile ? profile.fee_schedule.fee_rules.map(rule => ({ ...rule, rate: String(rule.rate), minimum: String(rule.minimum), fixed_amount: String(rule.fixed_amount), rounding_precision: rule.rounding_precision == null ? null : String(rule.rounding_precision), rows: pairs(rule.applicability) })) : [newRule()] };
}
export function hasStructuredMetadata(profile?: AccountProfile): boolean {
  return !!profile && Object.values(profile.metadata).some(value => typeof value !== "string");
}
function pairError(rows: Pair[]): string | null {
  const names = new Set<string>();
  for (const row of rows) {
    if (!row.key.trim()) return "请填写字段名称，或删除空行。";
    if (names.has(row.key.trim())) return `字段名称“${row.key.trim()}”重复。`;
    names.add(row.key.trim());
  }
  return null;
}
export function validateDraft(draft: Draft): Issue | null {
  for (const [field, value, label] of [["name", draft.name, "账户名称"], ["scheduleKey", draft.scheduleKey, "费用方案标识"]]) {
    if (!value.trim() || value.trim().length > 100) return { step: 0, field, message: `${label}需填写 1–100 个字符。` };
  }
  if (draft.scheduleKey.trim() === "zero_cost") return { step: 0, field: "scheduleKey", message: "zero_cost 仅用于内核测试，不能用于账户配置。" };
  if (!draft.rules.length) return { step: 1, field: "rules", message: "至少保留一条完整的费用规则。" };
  const keys = new Set<string>();
  for (const [index, rule] of draft.rules.entries()) {
    const issue = (field: string, message: string): Issue => ({ step: 1, rule: index, field, message: `费用规则 ${index + 1}：${message}` });
    for (const field of ["key", "category", "rounding_scope"] as const) {
      if (!rule[field]?.trim() || rule[field]!.trim().length > 100) return issue(field, "标识、费用类型和取整范围需填写 1–100 个字符。");
    }
    if (keys.has(rule.key.trim())) return issue("key", "规则标识不可重复。");
    keys.add(rule.key.trim());
    for (const field of ["rate", "minimum", "fixed_amount", "rounding_precision"] as const) {
      const value = (rule[field] ?? "").trim();
      // Validate decimal syntax without converting to binary floating point.
      if (!/^\+?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(value)) return issue(field, "请填写有效的非负十进制数。");
      if (field === "rounding_precision" && !/[1-9]/.test(value.split(/e/i)[0])) return issue(field, "取整精度必须大于 0。");
    }
    if (!rule.rounding_level) return issue("rounding_level", "请选择取整层级。");
    if (!rule.rounding_mode) return issue("rounding_mode", "请选择取整方式。");
    const error = pairError(rule.rows);
    if (error) return issue("conditions", error);
  }
  const error = pairError(draft.metadata);
  return error ? { step: 2, field: "metadata", message: error } : null;
}
function dictionary(rows: Pair[]): Record<string, string> {
  // Object.fromEntries preserves literal keys such as __proto__ safely and
  // retains empty values/whitespace instead of silently changing semantics.
  return Object.fromEntries(rows.map(row => [row.key.trim(), row.value]));
}
function canonical(value: unknown): string {
  return JSON.stringify(value, (_key, item: unknown) => item && typeof item === "object" && !Array.isArray(item) ? Object.fromEntries(Object.entries(item).sort(([a], [b]) => a.localeCompare(b))) : item);
}
export function payloadFor(draft: Draft, source?: AccountProfile): AccountProfilePayload {
  return { name: draft.name.trim(), status: draft.status, fee_schedule: {
    key: draft.scheduleKey.trim(), metadata: source?.fee_schedule.metadata ?? {},
    fee_rules: draft.rules.map(({ rows, ...rule }) => ({ ...rule, key: rule.key.trim(), category: rule.category.trim(), rounding_scope: rule.rounding_scope?.trim() || null, applicability: dictionary(rows) })),
  }, metadata: dictionary(draft.metadata) };
}
export function changedPayload(draft: Draft, source: AccountProfile): Partial<AccountProfilePayload> & { expected_version: number } {
  const next = payloadFor(draft, source), original = payloadFor(makeDraft(source), source);
  const patch: Partial<AccountProfilePayload> & { expected_version: number } = { expected_version: source.version };
  // Compare against the normalized initial editor values, omitting unchanged
  // fields so metadata-only edits cannot increment the fee schedule version.
  if (next.name !== original.name) patch.name = next.name;
  if (next.status !== original.status) patch.status = next.status;
  if (canonical(next.fee_schedule) !== canonical(original.fee_schedule)) patch.fee_schedule = next.fee_schedule;
  if (canonical(next.metadata) !== canonical(original.metadata)) patch.metadata = next.metadata;
  return patch;
}
export const sideLabel = (side: string | null) => side === "buy" ? "仅买入" : side === "sell" ? "仅卖出" : side ? `兼容方向（${side}）` : "买入和卖出";
export const formatTime = (value: string) => new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value));
