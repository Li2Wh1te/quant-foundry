import { cancellation, marketClient, marketError, type InstrumentPage, type MarketClient } from "../data";
import type { MarketFilters } from "../../../pages/market/marketPresentation";

export const ETF_CATALOG = "etf-current";
export const MAX_CATALOG_PAGES = 200;
export interface CatalogSlice { page: InstrumentPage; offset: number }
export const catalogSelection = (filters: MarketFilters) => JSON.stringify({ catalog_key: ETF_CATALOG,
  q: filters.keyword.trim(), exchange: filters.exchange, list_status: filters.listStatus, limit: filters.limit });

/** Keep only bounded signed cursors in this mounted page's memory, never results.
 * Shareable URLs contain position and filters. Returning to an uncached position
 * replays real server pages; it never sends a token through URL/storage.
 */
export function createCatalogPager(client: MarketClient = marketClient) {
  let selection = "", revision = 0, readToken: string | null = null;
  const cursors = new Map<number, string | undefined>([[0, undefined]]);
  const clear = () => { selection = ""; revision++; readToken = null; cursors.clear(); cursors.set(0, undefined); };
  return {
    clear,
    async load(filters: MarketFilters, signal: AbortSignal): Promise<CatalogSlice> {
      const next = catalogSelection(filters);
      if (selection !== next) { clear(); selection = next; }
      const epoch = revision;
      const target = filters.offset / filters.limit;
      if (!Number.isSafeInteger(target) || target < 0 || target >= MAX_CATALOG_PAGES) throw marketError(422, "QUERY_BUDGET_EXCEEDED");
      let index = cursors.has(target) ? target : 0;
      while (true) {
        if (signal.aborted || epoch !== revision) throw cancellation();
        const cursor = cursors.get(index);
        try {
          const page = await client.instruments({ catalog_key: ETF_CATALOG, q: filters.keyword.trim(),
            exchange: filters.exchange, list_status: filters.listStatus, limit: filters.limit, cursor, signal });
          if (signal.aborted || epoch !== revision) throw cancellation();
          if (index === 0 && readToken !== page.meta.read_token) { cursors.clear(); cursors.set(0, undefined); }
          if (index > 0 && readToken !== page.meta.read_token) { clear(); throw marketError(409, "DATA_CHANGED"); }
          readToken = page.meta.read_token;
          if (page.next_cursor && index + 1 < MAX_CATALOG_PAGES) cursors.set(index + 1, page.next_cursor);
          else cursors.delete(index + 1);
          if (index === target || !page.next_cursor) return { page, offset: index * filters.limit };
          index++;
        } catch (error) {
          // Network/timeout may retain only the currently displayed page in the
          // D08 session; any continuation must first be successfully refreshed.
          if (!signal.aborted && epoch === revision) clear();
          throw error;
        }
      }
    }
  };
}
