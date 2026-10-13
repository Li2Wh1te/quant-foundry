/** Current business contracts. Exact prices and quantities never pass through Number. */
export type DecimalText = string;
export type ReadToken = string;
export type DateText = string;
export type Availability = "available" | "restricted" | "unavailable" | "unknown";
export type Period = "day" | "week" | "month" | "year";
export type Adjustment = "none" | "pre" | "post";
export type AssetType = "etf" | "stock" | "index";
export interface Notice { code: string; message: string; scope?: string | null; }
export interface Capability {
  name: string; availability: Availability; request_checked: boolean; limitations: Notice[];
  mapping_implemented: boolean; metadata_known: boolean;
}
export interface Coverage {
  verified: boolean; state: "verified" | "partial" | "unknown";
  missing_dates: DateText[] | null; note: string;
}
export interface ReadMeta {
  read_token: ReadToken; read_at: string; business_as_of: DateText | null;
  scope: string; source_labels: string[]; coverage: Coverage; limitations: Notice[];
}
export interface Instrument {
  instrument_key: string; symbol: string; name: string | null; asset_type: AssetType;
  exchange: string | null; list_status: string | null; list_date: DateText | null;
  index_name: string | null; manager_name: string | null;
  management_fee: DecimalText | null; management_fee_unit: string | null; capabilities: Capability[];
}
export interface CatalogOption { catalog_key: string; label: string; asset_type: AssetType; capability: Capability; }
export interface CapabilitiesResult { catalogs: CatalogOption[]; capabilities: Capability[]; read_at: string; }
export interface InstrumentResolution { instrument_key: string; symbol: string; asset_type: AssetType; meta: ReadMeta; }
export interface InstrumentDetail { instrument: Instrument; meta: ReadMeta; }
export interface MarketOverview {
  catalog_key: string; total_instruments: number | null; listed_count: number | null;
  exchanges: string[]; first_list_date: DateText | null; latest_list_date: DateText | null; meta: ReadMeta;
}
export interface InstrumentPage {
  catalog_key: string; items: Instrument[]; total: number | null; next_cursor: string | null; meta: ReadMeta;
}
export interface CalendarDay {
  date: DateText; exchange: string; state: "open" | "closed" | "unknown";
  previous_trading_date: DateText | null; next_trading_date: DateText | null;
}
export interface CalendarResult { items: CalendarDay[]; meta: ReadMeta; }
export interface Units { price: string | null; volume: string | null; amount: string | null; }
export interface SeriesOption {
  series_key: string; label: string; instrument_key: string; input_frequency: "daily";
  periods: Period[]; adjustments: Adjustment[]; adjustment_capabilities: Partial<Record<Adjustment, Capability>>;
  price_basis: string; units: Units; capability: Capability;
}
export interface BarRequest {
  instrument_key: string; series_key: string; period: Period; adjustment: Adjustment;
  start?: DateText; end?: DateText; as_of?: DateText; limit?: number; cursor?: string;
}
export interface MarketBar {
  bucket_start: DateText; bucket_end: DateText; first_trade_date: DateText; last_trade_date: DateText;
  open: DecimalText | null; high: DecimalText | null; low: DecimalText | null; close: DecimalText | null;
  volume: DecimalText | null; amount: DecimalText | null; completion: "complete" | "partial" | "unknown";
}
export interface PriceBasis {
  adjustment: Adjustment; policy: string; as_of: DateText;
  anchor: { date: DateText; factor: DecimalText } | null; note: string;
}
export interface BarPage {
  items: MarketBar[]; series_key: string; instrument_key: string; period: Period; next_cursor: string | null;
  units: Units; window: { start: DateText | null; end: DateText; as_of: DateText }; basis: PriceBasis; meta: ReadMeta;
}
export interface FactorRow { trade_date: DateText; factor: DecimalText; }
export interface FactorPage { items: FactorRow[]; next_cursor: string | null; basis: PriceBasis; meta: ReadMeta; }
export interface MarketFailure { detail: { code: string; message: string; reason?: string; scope?: string }; }
export interface RequestOptions { signal?: AbortSignal; timeoutMs?: number; }
export interface CatalogRequest extends RequestOptions {
  catalog_key: string; q?: string; exchange?: string; list_status?: string; limit?: number; cursor?: string;
}
export interface CalendarRequest extends RequestOptions { exchange: string; start: DateText; end: DateText; }
export interface FactorRequest extends RequestOptions {
  instrument_key: string; series_key: string; start: DateText; end: DateText; limit?: number; cursor?: string;
}
