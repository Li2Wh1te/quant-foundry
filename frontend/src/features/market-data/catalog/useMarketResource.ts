import { useEffect, useRef, useState } from "react";
import { createMarketReadSession, isCancellation, MarketApiError, onMarketInvalidation,
  type MarketReadState } from "../data";

type Tagged<T> = { key: string; value: T };
const empty = <T,>(): MarketReadState<T> => ({ data: null, error: null, pending: false, stale: false, canContinue: false });

/** D09 view adapter over the D08 page-owned session, including other-panel denial. */
export function useMarketResource<T>(key: string, load: (signal: AbortSignal) => Promise<T>,
  revision: number, onUnauthorized: () => void, enabled = true): MarketReadState<T> {
  const session = useRef<ReturnType<typeof createMarketReadSession<Tagged<T>>> | null>(null);
  const active = useRef("");
  const [snapshot, setSnapshot] = useState<{ key: string; state: MarketReadState<Tagged<T>> }>({ key: "", state: empty() });
  useEffect(() => {
    const current = createMarketReadSession<Tagged<T>>();
    session.current = current;
    const unsubscribe = current.subscribe(state => setSnapshot({ key: active.current, state }));
    const stop = onMarketInvalidation(reason => { if (reason === "authentication") onUnauthorized(); });
    return () => { unsubscribe(); stop(); current.dispose(); session.current = null; };
  }, [onUnauthorized]);
  useEffect(() => {
    const current = session.current;
    if (!current) return;
    active.current = key;
    if (!enabled) { current.clear(); return; }
    void current.read(key, async signal => ({ key, value: await load(signal) })).catch(error => {
      if (!isCancellation(error) && error instanceof MarketApiError && error.status === 401) onUnauthorized();
    });
    // A new read aborts the previous one and retains only same-condition data.
    // Disposal owns unmount cancellation; refresh must not clear stale candidates.
  }, [key, load, revision, enabled, onUnauthorized]);
  const state = snapshot.key === key ? snapshot.state : empty<Tagged<T>>();
  return { ...state, pending: enabled && (snapshot.key !== key || state.pending),
    data: enabled && state.data?.key === key ? state.data.value : null,
    canContinue: enabled && state.data?.key === key && state.canContinue };
}
