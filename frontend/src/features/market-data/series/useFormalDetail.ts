import { useCallback, useEffect, useRef, useState } from "react";
import { createMarketReadSession, isCancellation, marketClient, type BarPage, type InstrumentDetail,
  type InstrumentResolution, type MarketReadState, type SeriesOption, type Adjustment, type Period } from "../data";
import { createSeriesWindow } from "./window";

type DetailIdentity = Pick<InstrumentResolution, "instrument_key" | "symbol"> & { requestedSymbol: string };

const empty = <T,>(): MarketReadState<T> => ({ data: null, error: null, pending: false, stale: false, canContinue: false });
export function useFormalDetail(symbol: string, request: { period: Period; adjustment: Adjustment; start?: string;
  end: string; asOf: string; refresh: number }, onError: (error: unknown) => void) {
  const [identity, setIdentity] = useState(empty<DetailIdentity>), [metadata, setMetadata] = useState(empty<InstrumentDetail>);
  const [options, setOptions] = useState(empty<SeriesOption[]>), [prices, setPrices] = useState(empty<BarPage>);
  const active = useRef<ReturnType<typeof createSeriesWindow> | null>(null);
  const refreshMetadata = useRef<(() => void) | null>(null), lastRefresh = useRef(request.refresh);
  const readCapabilities = useRef<((key: string, asOf: string) => void) | null>(null);
  const factorError = useCallback((error: unknown) => {
    void active.current?.factorFailed(error);
    onError(error);
  }, [onError]);
  useEffect(() => {
    const resolver = createMarketReadSession<DetailIdentity>(), profile = createMarketReadSession<InstrumentDetail>();
    const capabilities = createMarketReadSession<SeriesOption[]>(), window = createSeriesWindow();
    active.current = window;
    readCapabilities.current = (key, asOf) => {
      void capabilities.read(JSON.stringify({ key, asOf }), signal => marketClient.series(key, { signal, as_of: asOf })).catch(error => {
        if (!isCancellation(error)) onError(error);
      });
    };
    const subscriptions = [resolver.subscribe(setIdentity), profile.subscribe(setMetadata),
      capabilities.subscribe(setOptions), window.session.subscribe(setPrices)];
    const report = (error: unknown) => { if (!isCancellation(error)) onError(error); };
    const read = () => {
      void resolver.read(JSON.stringify({ symbol, asset_type: "etf" }), signal => symbol.startsWith("instrument:")
        ? Promise.resolve({ instrument_key: symbol, symbol, requestedSymbol: symbol })
        : marketClient.resolve(symbol, "etf", { signal }).then(result => ({ ...result, requestedSymbol: symbol })))
        .then(result => {
          void profile.read(result.instrument_key, signal => marketClient.instrument(result.instrument_key, { signal })).catch(report);
        }).catch(report);
    };
    refreshMetadata.current = read; lastRefresh.current = request.refresh; read();
    return () => {
      refreshMetadata.current = null; active.current = null; readCapabilities.current = null;
      subscriptions.forEach(stop => stop()); resolver.dispose(); profile.dispose(); capabilities.dispose(); window.dispose();
    };
  }, [symbol, onError]);
  useEffect(() => {
    if (lastRefresh.current !== request.refresh) { lastRefresh.current = request.refresh; refreshMetadata.current?.(); }
  }, [request.refresh]);
  const key = identity.data?.instrument_key;
  useEffect(() => {
    if (key) readCapabilities.current?.(key, request.asOf);
  }, [key, request.asOf, request.refresh]);
  useEffect(() => {
    if (!key) { active.current?.clear(); return; }
    void active.current?.load({ instrument_key: key, series_key: "etf-daily-local", period: request.period,
      adjustment: request.adjustment, start: request.start, end: request.end, as_of: request.asOf, limit: 512 })
      .catch(error => { if (!isCancellation(error)) onError(error); });
  }, [key, request.period, request.adjustment, request.start, request.end, request.asOf, request.refresh, onError]);
  return { identity, metadata, options, prices, factorError,
    earlier: () => { void active.current?.earlier().catch(error => { if (!isCancellation(error)) onError(error); }); },
    release: () => { active.current?.clear(); } };
}
