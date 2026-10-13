import { createMarketReadSession, marketClient, marketError, type Adjustment, type FactorPage, type FactorRequest, type MarketClient } from "../data";

export const MAX_RESIDENT_FACTORS = 10_000;
export function joinFactorPages(current: FactorPage, earlier: FactorPage): FactorPage {
  if (current.meta.read_token !== earlier.meta.read_token || JSON.stringify(current.basis) !== JSON.stringify(earlier.basis))
    throw marketError(409, "DATA_CHANGED");
  const rows = new Map(current.items.map(row => [row.trade_date, row]));
  for (const row of earlier.items) {
    const existing = rows.get(row.trade_date);
    if (existing && existing.factor !== row.factor) throw marketError(409, "DATA_CHANGED");
    rows.set(row.trade_date, row);
  }
  const items = [...rows.values()].sort((a, b) => a.trade_date.localeCompare(b.trade_date));
  if (items.length > MAX_RESIDENT_FACTORS) throw marketError(422, "QUERY_BUDGET_EXCEEDED");
  return { ...earlier, items, meta: { ...earlier.meta, business_as_of: current.meta.business_as_of,
    scope: `${earlier.meta.scope} · 已加载 ${items[0]?.trade_date ?? "—"} 至 ${items.at(-1)?.trade_date ?? "—"}` } };
}
export function createFactorWindow(client: MarketClient = marketClient) {
  const session = createMarketReadSession<FactorPage>();
  let request: FactorRequest | null = null, condition = "", used = new Set<string>();
  return {
    session,
    load(next: FactorRequest, adjustment: Adjustment) {
      request = { ...next, cursor: undefined }; used.clear();
      condition = JSON.stringify({ ...request, adjustment });
      return session.read(condition, signal => client.factors({ ...request!, signal }));
    },
    earlier() {
      const page = session.state.data, cursor = page?.next_cursor;
      if (!request || !page || !cursor || !session.state.canContinue || session.state.pending || used.has(cursor)) return Promise.resolve();
      used.add(cursor);
      return session.read(condition, async signal => joinFactorPages(page,
        await client.factors({ ...request!, cursor, signal })));
    },
    dispose() { request = null; used.clear(); session.dispose(); }
  };
}
export function defaultFactorStart(end: string): string {
  const day = new Date(`${end}T00:00:00Z`);
  day.setUTCDate(day.getUTCDate() - 365);
  return day.toISOString().slice(0, 10);
}
