import { createMarketReadSession, isCancellation, marketClient, marketError, retainStale, type BarPage, type BarRequest, type MarketClient } from "../data";

export const MAX_RESIDENT_BARS = 10_000;
/** Render gate closes the pre-effect frame when a cutoff/basis/period changes. */
export function matchesBarWindow(page: BarPage, request: Pick<BarRequest, "instrument_key" | "period" | "adjustment" | "start" | "end" | "as_of">): boolean {
  return page.instrument_key === request.instrument_key && page.period === request.period && page.basis.adjustment === request.adjustment
    && page.window.start === (request.start ?? null) && page.window.end === request.end && page.window.as_of === request.as_of;
}
/** Pure join: never splice different reads, buckets, windows or bases. */
export function joinBarPages(current: BarPage, earlier: BarPage): BarPage {
  if (current.meta.read_token !== earlier.meta.read_token || current.instrument_key !== earlier.instrument_key
    || current.series_key !== earlier.series_key || current.period !== earlier.period
    || JSON.stringify(current.window) !== JSON.stringify(earlier.window) || JSON.stringify(current.basis) !== JSON.stringify(earlier.basis)
    || JSON.stringify(current.units) !== JSON.stringify(earlier.units)) throw marketError(409, "DATA_CHANGED");
  const byBucket = new Map(current.items.map(bar => [bar.bucket_start, bar]));
  for (const bar of earlier.items) {
    const prior = byBucket.get(bar.bucket_start);
    if (prior && JSON.stringify(prior) !== JSON.stringify(bar)) throw marketError(409, "DATA_CHANGED");
    byBucket.set(bar.bucket_start, bar);
  }
  const items = [...byBucket.values()].sort((a, b) => a.bucket_start.localeCompare(b.bucket_start));
  if (items.length > MAX_RESIDENT_BARS) throw marketError(422, "QUERY_BUDGET_EXCEEDED");
  const verified = current.meta.coverage.verified && earlier.meta.coverage.verified;
  const missing_dates = verified ? [...new Set([...(current.meta.coverage.missing_dates ?? []), ...(earlier.meta.coverage.missing_dates ?? [])])].sort() : null;
  return { ...earlier, items, meta: { ...earlier.meta, scope: `${earlier.instrument_key} · ${earlier.period} · 已加载 ${items[0]?.first_trade_date ?? "—"} 至 ${current.window.end}`, business_as_of: current.meta.business_as_of,
    limitations: [...new Map([...current.meta.limitations, ...earlier.meta.limitations].map(item => [item.code, item])).values()],
    coverage: { ...earlier.meta.coverage, verified, missing_dates,
      state: verified ? missing_dates?.length ? "partial" : "verified" : "unknown" } } };
}
export function createSeriesWindow(client: MarketClient = marketClient) {
  const session = createMarketReadSession<BarPage>();
  let active: BarRequest | null = null, condition = "", used = new Set<string>();
  return {
    session,
    async load(request: BarRequest) {
      // The page chooses/fixes the exchange-business cutoff before initiating I/O.
      if (!request.end || !request.as_of) throw marketError(400, "INVALID_REQUEST");
      active = { ...request, cursor: undefined }; condition = JSON.stringify(active); used = new Set();
      return session.read(condition, signal => client.bars(active!, { signal }));
    },
    async earlier() {
      const page = session.state.data, cursor = page?.next_cursor;
      if (!active || !page || !cursor || !session.state.canContinue || session.state.pending || used.has(cursor)) return;
      used.add(cursor);
      try { return await session.read(condition, async signal => joinBarPages(page,
        await client.bars({ ...active!, cursor }, { signal }))); }
      catch (error) { throw error; }
    },
    factorFailed(error: unknown) {
      if (!active || active.adjustment === "none" || isCancellation(error)) return;
      // A dependent panel failure uses the same session failure rules. Clear
      // invalid content immediately; network/timeout retains only marked stale.
      if (!retainStale(error)) session.clear();
      used.clear();
      return session.read(condition, async () => { throw error; }).catch(() => undefined);
    },
    clear() { active = null; used.clear(); session.clear(); },
    dispose() { active = null; used.clear(); session.dispose(); }
  };
}
export function shanghaiBusinessDate(now = new Date()): string {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(now);
  const field = (name: string) => parts.find(part => part.type === name)?.value;
  return `${field("year")}-${field("month")}-${field("day")}`;
}
