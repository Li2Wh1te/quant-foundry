import { invalidResponse } from "./errors";
import type { Adjustment, AssetType, BarPage, CalendarResult, CapabilitiesResult, Capability, Coverage,
  FactorPage, Instrument, InstrumentDetail, InstrumentPage, InstrumentResolution, MarketBar, MarketOverview,
  Notice, Period, PriceBasis, ReadMeta, SeriesOption, Units } from "./types";

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalidResponse();
  return value as Record<string, unknown>;
}
function text(value: unknown, max = 256): string {
  if (typeof value !== "string" || !value.trim() || value.length > max || /[\u0000-\u001f]/.test(value)) invalidResponse();
  return value;
}
function nullable<T>(value: unknown, parse: (v: unknown) => T): T | null {
  return value === null ? null : parse(value);
}
function boolean(value: unknown): boolean { if (typeof value !== "boolean") invalidResponse(); return value; }
function enumeration<T extends string>(value: unknown, values: readonly T[]): T {
  if (typeof value !== "string" || !values.includes(value as T)) invalidResponse();
  return value as T;
}
function array<T>(value: unknown, parse: (v: unknown) => T, max: number): T[] {
  if (!Array.isArray(value) || value.length > max) invalidResponse();
  return value.map(v => parse(v));
}
function count(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) invalidResponse(); return value;
}
function unique<T>(values: T[], key: (v: T) => unknown = v => v): T[] {
  if (new Set(values.map(key)).size !== values.length) invalidResponse(); return values;
}
export function dateText(value: unknown): string {
  const result = text(value, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(result)) invalidResponse();
  const parsed = new Date(`${result}T00:00:00Z`);
  if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== result) invalidResponse();
  return result;
}
function instant(value: unknown): string {
  const result = text(value, 64);
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/.test(result)
    || !Number.isFinite(Date.parse(result))) invalidResponse(); return result;
}
export function decimalText(value: unknown): string {
  const result = text(value);
  if (!/^-?(0|[1-9][0-9]*)(\.[0-9]+)?$/.test(result)) invalidResponse(); return result;
}
export function cursorText(value: unknown): string {
  const result = text(value, 8192);
  if (!/^[A-Za-z0-9_-]+={0,2}$/.test(result)) invalidResponse(); return result;
}
export function keyText(value: unknown): string {
  const result = text(value);
  if (!/^[A-Za-z0-9][A-Za-z0-9:._-]*$/.test(result)) invalidResponse(); return result;
}
export const assetType = (v: unknown): AssetType => enumeration(v, ["etf", "stock", "index"]);
export const period = (v: unknown): Period => enumeration(v, ["day", "week", "month", "year"]);
export const adjustment = (v: unknown): Adjustment => enumeration(v, ["none", "pre", "post"]);
function notice(v: unknown): Notice {
  const o = object(v); return { code: keyText(o.code), message: text(o.message, 1024),
    ...(o.scope === undefined ? {} : { scope: nullable(o.scope, text) }) };
}
export function capability(v: unknown): Capability {
  const o = object(v); return { name: keyText(o.name), availability: enumeration(o.availability,
    ["available", "restricted", "unavailable", "unknown"]), request_checked: boolean(o.request_checked),
    mapping_implemented: boolean(o.mapping_implemented), metadata_known: boolean(o.metadata_known),
    limitations: array(o.limitations, notice, 64) };
}
function coverage(v: unknown): Coverage {
  const o = object(v), result: Coverage = { verified: boolean(o.verified),
    state: enumeration(o.state, ["verified", "partial", "unknown"]),
    missing_dates: nullable(o.missing_dates, v => unique(array(v, dateText, 2000))), note: text(o.note, 1024) };
  if (result.verified !== (result.state === "verified") || !result.verified && result.missing_dates?.length === 0) invalidResponse();
  return result;
}
export function readMeta(v: unknown): ReadMeta {
  const o = object(v); return { read_token: keyText(o.read_token), read_at: instant(o.read_at),
    business_as_of: nullable(o.business_as_of, dateText), scope: text(o.scope),
    source_labels: array(o.source_labels, text, 16), coverage: coverage(o.coverage), limitations: array(o.limitations, notice, 64) };
}
export function parseCapabilities(v: unknown): CapabilitiesResult {
  const o = object(v); return { catalogs: unique(array(o.catalogs, value => { const c = object(value);
    return { catalog_key: keyText(c.catalog_key), label: text(c.label), asset_type: assetType(c.asset_type),
      capability: capability(c.capability) }; }, 32), c => c.catalog_key),
    capabilities: unique(array(o.capabilities, capability, 64), c => c.name), read_at: instant(o.read_at) };
}
export function parseResolution(v: unknown): InstrumentResolution {
  const o = object(v); return { instrument_key: keyText(o.instrument_key), symbol: text(o.symbol, 64),
    asset_type: assetType(o.asset_type), meta: readMeta(o.meta) };
}
function instrument(v: unknown): Instrument {
  const o = object(v); return { instrument_key: keyText(o.instrument_key), symbol: text(o.symbol, 64),
    name: nullable(o.name, text), asset_type: assetType(o.asset_type), exchange: nullable(o.exchange, text),
    list_status: nullable(o.list_status, text), list_date: nullable(o.list_date, dateText),
    index_name: nullable(o.index_name, text), manager_name: nullable(o.manager_name, text),
    management_fee: nullable(o.management_fee, decimalText), management_fee_unit: nullable(o.management_fee_unit, text),
    capabilities: unique(array(o.capabilities, capability, 64), c => c.name) };
}
export function parseDetail(v: unknown): InstrumentDetail {
  const o = object(v); return { instrument: instrument(o.instrument), meta: readMeta(o.meta) };
}
export function parseInstruments(v: unknown): InstrumentPage {
  const o = object(v), result = { catalog_key: keyText(o.catalog_key),
    items: unique(array(o.items, instrument, 200), i => i.instrument_key), total: nullable(o.total, count),
    next_cursor: nullable(o.next_cursor, cursorText), meta: readMeta(o.meta) };
  if (result.total !== null && result.total < result.items.length || result.next_cursor && !result.items.length) invalidResponse();
  return result;
}
export function parseOverview(v: unknown): MarketOverview {
  const o = object(v), result: MarketOverview = { catalog_key: keyText(o.catalog_key),
    total_instruments: nullable(o.total_instruments, count), listed_count: nullable(o.listed_count, count),
    exchanges: unique(array(o.exchanges, text, 64)), first_list_date: nullable(o.first_list_date, dateText),
    latest_list_date: nullable(o.latest_list_date, dateText), meta: readMeta(o.meta) };
  if (result.total_instruments !== null && result.listed_count !== null && result.listed_count > result.total_instruments) invalidResponse();
  if (result.first_list_date && result.latest_list_date && result.first_list_date > result.latest_list_date) invalidResponse();
  return result;
}
export function parseCalendar(v: unknown): CalendarResult {
  const o = object(v); return { items: unique(array(o.items, value => { const d = object(value);
    return { date: dateText(d.date), exchange: text(d.exchange), state: enumeration(d.state, ["open", "closed", "unknown"]),
      previous_trading_date: nullable(d.previous_trading_date, dateText), next_trading_date: nullable(d.next_trading_date, dateText) };
  }, 2000), d => `${d.exchange}:${d.date}`), meta: readMeta(o.meta) };
}
function units(v: unknown): Units {
  const o = object(v); return { price: nullable(o.price, text), volume: nullable(o.volume, text), amount: nullable(o.amount, text) };
}
export function parseSeries(v: unknown): SeriesOption[] {
  return unique(array(v, value => { const o = object(value), caps = object(o.adjustment_capabilities);
    if (Object.keys(caps).some(key => !["none", "pre", "post"].includes(key))) invalidResponse();
    const adjustments = unique(array(o.adjustments, adjustment, 3));
    const adjustment_capabilities: SeriesOption["adjustment_capabilities"] = {};
    for (const [k, c] of Object.entries(caps)) adjustment_capabilities[adjustment(k)] = capability(c);
    if (adjustments.some(k => adjustment_capabilities[k]?.availability !== "available")) invalidResponse();
    return { series_key: keyText(o.series_key), label: text(o.label), instrument_key: keyText(o.instrument_key),
      input_frequency: enumeration(o.input_frequency, ["daily"]), periods: unique(array(o.periods, period, 4)),
      adjustments, adjustment_capabilities, price_basis: text(o.price_basis), units: units(o.units), capability: capability(o.capability) };
  }, 32), s => s.series_key);
}
function basis(v: unknown): PriceBasis {
  const o = object(v), result: PriceBasis = { adjustment: adjustment(o.adjustment), policy: text(o.policy), as_of: dateText(o.as_of),
    anchor: nullable(o.anchor, value => { const a = object(value); return { date: dateText(a.date), factor: decimalText(a.factor) }; }), note: text(o.note, 1024) };
  if (result.adjustment !== "none" && result.anchor === null || result.anchor && result.anchor.date > result.as_of) invalidResponse();
  return result;
}
function bar(v: unknown): MarketBar {
  const o = object(v), result: MarketBar = { bucket_start: dateText(o.bucket_start), bucket_end: dateText(o.bucket_end),
    first_trade_date: dateText(o.first_trade_date), last_trade_date: dateText(o.last_trade_date),
    open: nullable(o.open, decimalText), high: nullable(o.high, decimalText), low: nullable(o.low, decimalText), close: nullable(o.close, decimalText),
    volume: nullable(o.volume, decimalText), amount: nullable(o.amount, decimalText), completion: enumeration(o.completion, ["complete", "partial", "unknown"]) };
  if (!(result.bucket_start <= result.first_trade_date && result.first_trade_date <= result.last_trade_date
    && result.last_trade_date <= result.bucket_end)) invalidResponse(); return result;
}
export function parseBars(v: unknown): BarPage {
  const o = object(v), w = object(o.window), result: BarPage = { items: array(o.items, bar, 2000), series_key: keyText(o.series_key),
    instrument_key: keyText(o.instrument_key), period: period(o.period), next_cursor: nullable(o.next_cursor, cursorText),
    units: units(o.units), window: { start: nullable(w.start, dateText), end: dateText(w.end), as_of: dateText(w.as_of) },
    basis: basis(o.basis), meta: readMeta(o.meta) };
  if (result.window.end > result.window.as_of || result.window.start && result.window.start > result.window.end
    || result.basis.as_of !== result.window.as_of || result.next_cursor && !result.items.length) invalidResponse();
  if (result.items.some((b, i) => b.last_trade_date > result.window.end
    || result.window.start && b.first_trade_date < result.window.start
    || i > 0 && result.items[i - 1].bucket_end >= b.bucket_start)) invalidResponse(); return result;
}
export function parseFactors(v: unknown): FactorPage {
  const o = object(v), result: FactorPage = { items: array(o.items, value => { const f = object(value);
    return { trade_date: dateText(f.trade_date), factor: decimalText(f.factor) }; }, 2000),
    next_cursor: nullable(o.next_cursor, cursorText), basis: basis(o.basis), meta: readMeta(o.meta) };
  if (result.next_cursor && !result.items.length || result.items.some((f, i) => f.trade_date > result.basis.as_of
    || i > 0 && result.items[i - 1].trade_date >= f.trade_date)) invalidResponse(); return result;
}
