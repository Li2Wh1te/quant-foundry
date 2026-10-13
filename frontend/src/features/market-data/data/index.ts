export type * from "./types";
export { createMarketClient, marketClient } from "./client";
export type { MarketClient } from "./client";
export { marketTransport } from "./transport";
export type { MarketTransport } from "./transport";
export { MarketApiError, marketError, cancellation, isCancellation, retainStale } from "./errors";
export { createMarketReadSession } from "./session";
export type { MarketReadState } from "./session";
/** Display exact text in tables/tooltips/copy; Number conversion belongs only to D10's checked plotting boundary. */
export function exactMarketValue(value: string | null): string { return value === null ? "—" : value; }
