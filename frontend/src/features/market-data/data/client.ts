import { cancellation, invalidRequest, invalidResponse } from "./errors";
import { marketTransport, type MarketTransport } from "./transport";
import { adjustment, assetType, cursorText, dateText, keyText, parseBars, parseCalendar, parseCapabilities,
  parseDetail, parseFactors, parseInstruments, parseOverview, parseResolution, parseSeries, period } from "./validation";
import type { AssetType, BarPage, BarRequest, CalendarRequest, CalendarResult, CapabilitiesResult, CatalogRequest,
  FactorPage, FactorRequest, InstrumentDetail, InstrumentPage, InstrumentResolution, MarketOverview,
  RequestOptions, SeriesOption } from "./types";

function requestValue<T>(parse: (v: unknown) => T, value: unknown): T {
  try { return parse(value); } catch { invalidRequest(); }
}
function key(value: unknown): string { return requestValue(keyText, value); }
function cursor(value: unknown): string { return requestValue(cursorText, value); }
function date(value: unknown): string { return requestValue(dateText, value); }
function limit(value: unknown, fallback: number, maximum: number): number {
  if (value === undefined) return fallback;
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 1 || value > maximum) invalidRequest();
  return value;
}
function range(start: string, end: string) { if (date(start) > date(end)) invalidRequest(); }

export interface MarketClient {
  capabilities(options?: RequestOptions): Promise<CapabilitiesResult>;
  resolve(symbol: string, asset_type?: AssetType, options?: RequestOptions): Promise<InstrumentResolution>;
  instruments(request: CatalogRequest): Promise<InstrumentPage>;
  instrument(instrument_key: string, options?: RequestOptions): Promise<InstrumentDetail>;
  overview(catalog_key: string, options?: RequestOptions): Promise<MarketOverview>;
  calendar(request: CalendarRequest): Promise<CalendarResult>;
  calendarDay(exchange: string, date: string, options?: RequestOptions): Promise<CalendarResult>;
  series(instrument_key: string, options?: RequestOptions): Promise<SeriesOption[]>;
  bars(request: BarRequest, options?: RequestOptions): Promise<BarPage>;
  factors(request: FactorRequest): Promise<FactorPage>;
}

export function createMarketClient(transport: MarketTransport = marketTransport): MarketClient {
  const get = async <T>(path: string, parse: (v: unknown) => T, options: RequestOptions = {}): Promise<T> => {
    if (options.signal?.aborted) throw cancellation();
    const raw = await transport(path, options);
    if (options.signal?.aborted) throw cancellation();
    return parse(raw);
  };
  return {
    capabilities: (options = {}) => get("/capabilities", parseCapabilities, options),
    async resolve(symbol, asset_type = "etf", options = {}) {
      if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(symbol)) invalidRequest();
      const type = requestValue(assetType, asset_type);
      const q = new URLSearchParams({ symbol, asset_type: type });
      const result = await get(`/instruments/resolve?${q}`, parseResolution, options);
      if (result.asset_type !== type) invalidResponse(); return result;
    },
    async instruments(request) {
      const q = new URLSearchParams({ catalog_key: key(request.catalog_key), limit: String(limit(request.limit, 50, 200)) });
      for (const name of ["q", "exchange", "list_status"] as const) {
        const v = request[name];
        if (v !== undefined) {
          if (typeof v !== "string" || v.length > 128 || /[\u0000-\u001f]/.test(v)) invalidRequest();
          q.set(name, v);
        }
      }
      if (request.cursor !== undefined) q.set("cursor", cursor(request.cursor));
      const result = await get(`/instruments?${q}`, parseInstruments, request);
      if (result.catalog_key !== request.catalog_key || result.items.length > Number(q.get("limit"))) invalidResponse(); return result;
    },
    async instrument(instrument_key, options = {}) {
      const result = await get(`/instruments/${encodeURIComponent(key(instrument_key))}`, parseDetail, options);
      if (result.instrument.instrument_key !== instrument_key) invalidResponse(); return result;
    },
    async overview(catalog_key, options = {}) {
      const result = await get(`/overview?${new URLSearchParams({ catalog_key: key(catalog_key) })}`, parseOverview, options);
      if (result.catalog_key !== catalog_key) invalidResponse(); return result;
    },
    async calendar(request) {
      range(request.start, request.end);
      const result = await get(`/calendar?${new URLSearchParams({ exchange: key(request.exchange), start: request.start, end: request.end })}`, parseCalendar, request);
      if (result.items.some(d => d.exchange !== request.exchange || d.date < request.start || d.date > request.end)) invalidResponse(); return result;
    },
    async calendarDay(exchange, day, options = {}) {
      const result = await get(`/calendar/${encodeURIComponent(key(exchange))}/${date(day)}`, parseCalendar, options);
      if (result.items.length !== 1 || result.items[0].exchange !== exchange || result.items[0].date !== day) invalidResponse(); return result;
    },
    async series(instrument_key, options = {}) {
      const result = await get(`/instruments/${encodeURIComponent(key(instrument_key))}/series`, parseSeries, options);
      if (result.some(s => s.instrument_key !== instrument_key)) invalidResponse(); return result;
    },
    async bars(request, options = {}) {
      const p = requestValue(period, request.period), basis = requestValue(adjustment, request.adjustment);
      const q = new URLSearchParams({ series_key: key(request.series_key), period: p, adjustment: basis,
        limit: String(limit(request.limit, 512, 2000)) });
      for (const name of ["start", "end", "as_of"] as const) if (request[name] !== undefined) q.set(name, date(request[name]));
      if (request.start && request.end) range(request.start, request.end);
      if (request.end && request.as_of && request.end > request.as_of) invalidRequest();
      // A continuation belongs to one fixed server-returned business window.
      if (request.cursor !== undefined) {
        if (!request.end || !request.as_of) invalidRequest();
        q.set("cursor", cursor(request.cursor));
      }
      const result = await get(`/instruments/${encodeURIComponent(key(request.instrument_key))}/bars?${q}`, parseBars, options);
      if (result.instrument_key !== request.instrument_key || result.series_key !== request.series_key || result.period !== p
        || result.basis.adjustment !== basis || result.items.length > Number(q.get("limit"))
        || request.end && result.window.end !== request.end || request.as_of && result.window.as_of !== request.as_of) invalidResponse(); return result;
    },
    async factors(request) {
      range(request.start, request.end);
      if (request.as_of && (requestValue(dateText, request.as_of) < request.end)) invalidRequest();
      const q = new URLSearchParams({ series_key: key(request.series_key), start: request.start, end: request.end,
        limit: String(limit(request.limit, 512, 2000)) });
      if (request.as_of) q.set("as_of", request.as_of);
      if (request.cursor !== undefined) q.set("cursor", cursor(request.cursor));
      const result = await get(`/instruments/${encodeURIComponent(key(request.instrument_key))}/factors?${q}`, parseFactors, request);
      if (result.items.length > Number(q.get("limit")) || result.items.some(f => f.trade_date < request.start || f.trade_date > request.end)
        || result.basis.as_of !== (request.as_of ?? request.end)) invalidResponse(); return result;
    }
  };
}
export const marketClient = createMarketClient();
