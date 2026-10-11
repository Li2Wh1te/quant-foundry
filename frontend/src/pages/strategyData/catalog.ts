/** S3 SDK documentation only; data availability is checked by the run service. */
export const API_GROUPS = [
  { id: "context", label: "上下文", index: "CONTEXT" },
  { id: "data", label: "市场数据", index: "MARKET DATA" },
  { id: "clock", label: "策略时钟", index: "CLOCK" },
  { id: "universe", label: "研究与标的池", index: "RESEARCH" },
  { id: "orders", label: "订单与成交", index: "ORDERS" },
  { id: "run", label: "运行服务", index: "RUN SERVICE" }
] as const;
export type ApiGroup = typeof API_GROUPS[number]["id"];
export type Property = readonly [string, string];
export interface ApiEntry {
  id: string; group: ApiGroup; signature: string; summary: string; badge: string;
  meta: Property[]; params: Property[]; returns: Property[]; notes: string[];
  examples: { title: string; code: string }[]; boundary: Property[];
}
import { S3_API_ENTRIES } from "./s3";
export const API_ENTRIES: ApiEntry[] = S3_API_ENTRIES;

export const TABS = [{ id: "reference", label: "说明" }, { id: "example", label: "示例" }, { id: "boundary", label: "时间边界" }] as const;
export interface CatalogState { query: string; type: ApiGroup | "all"; api: string; tab: typeof TABS[number]["id"]; example: number }
export const DEFAULT_STATE: CatalogState = { query: "", type: "all", api: "get_price", tab: "reference", example: 0 };

/** Only known catalog values are restored; stale saved entries cannot blank the
 * page after a future documentation update or select an out-of-range example. */
export function restoreCatalogState(value: unknown): CatalogState {
  if (!value || typeof value !== "object") return { ...DEFAULT_STATE };
  const state = value as Partial<CatalogState>;
  const entry = API_ENTRIES.find(item => item.id === state.api) ?? API_ENTRIES[0];
  return {
    query: typeof state.query === "string" ? state.query : "",
    type: API_GROUPS.some(group => group.id === state.type) ? state.type! : "all",
    api: entry.id,
    tab: TABS.some(tab => tab.id === state.tab) ? state.tab! : "reference",
    example: Number.isInteger(state.example) && state.example! >= 0 && state.example! < entry.examples.length ? state.example! : 0
  };
}
export function filterCatalog(query: string, type: CatalogState["type"]) {
  const text = query.trim().toLowerCase();
  return API_ENTRIES.filter(entry => (type === "all" || entry.group === type)
    && `${entry.signature} ${entry.summary} ${entry.badge}`.toLowerCase().includes(text));
}
