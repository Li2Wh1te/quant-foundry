import { onDataAssetsInvalidation } from "../../data-assets/data/lifecycle";
import { readApiToken } from "../../../auth/tokenStorage";
import { cancellation, isCancellation, MarketApiError, marketError, retainStale } from "./errors";

export interface MarketReadState<T> {
  data: T | null; error: MarketApiError | null; pending: boolean; stale: boolean; canContinue: boolean;
}
/** One page-owned in-memory read. Dispose on unmount; never persist tokens/results. */
export function createMarketReadSession<T>() {
  let state: MarketReadState<T> = { data: null, error: null, pending: false, stale: false, canContinue: false };
  let controller: AbortController | null = null, sequence = 0, invalidation = 0, condition: string | null = null, disposed = false;
  let credential = readApiToken();
  const listeners = new Set<(state: MarketReadState<T>) => void>();
  const publish = (next: MarketReadState<T>) => { state = next; for (const listener of listeners) listener(state); };
  const clear = () => {
    sequence++; controller?.abort(); controller = null; condition = null;
    publish({ data: null, error: null, pending: false, stale: false, canContinue: false });
  };
  const checkCredential = () => {
    const current = readApiToken();
    if (current !== credential) { credential = current; clear(); }
  };
  const stop = onDataAssetsInvalidation(reason => {
    if (reason === "session") clear();
    else {
      // The originating transport must still deliver its 401/403 to the
      // existing auth flow. Other transports are cancelled by the shared bus.
      invalidation++;
      publish({ data: null, error: marketError(reason === "authentication" ? 401 : 403),
        pending: false, stale: false, canContinue: false });
    }
  });
  return {
    get state() { checkCredential(); return state; },
    subscribe(listener: (state: MarketReadState<T>) => void) {
      if (disposed) throw cancellation(); listeners.add(listener); return () => { listeners.delete(listener); };
    },
    async read(key: string, load: (signal: AbortSignal) => Promise<T>): Promise<T> {
      if (disposed) throw cancellation();
      checkCredential();
      if (!key || key.length > 4096) throw marketError(400, "INVALID_REQUEST");
      controller?.abort(); controller = new AbortController();
      const current = controller, id = ++sequence, revision = invalidation, owner = credential, same = condition === key;
      condition = key;
      publish({ data: same ? state.data : null, error: null, pending: true, stale: false, canContinue: false });
      let rejectAbort: (() => void) | undefined;
      const cancelled = new Promise<never>((_resolve, reject) => {
        rejectAbort = () => reject(cancellation());
        current.signal.addEventListener("abort", rejectAbort, { once: true });
      });
      try {
        const result = await Promise.race([load(current.signal), cancelled]);
        checkCredential();
        if (disposed || id !== sequence || current.signal.aborted || revision !== invalidation) throw cancellation();
        publish({ data: result, error: null, pending: false, stale: false, canContinue: true }); return result;
      } catch (problem) {
        if (owner !== readApiToken()) { checkCredential(); throw cancellation(); }
        if (disposed || id !== sequence || current.signal.aborted) throw cancellation();
        const denial = problem instanceof MarketApiError && [401, 403].includes(problem.status);
        if (revision !== invalidation && !denial) throw cancellation();
        if (isCancellation(problem)) { clear(); throw problem; }
        const error = problem instanceof MarketApiError ? problem : marketError(502, "MARKET_UNAVAILABLE");
        const stale = same && state.data !== null && retainStale(error);
        publish({ data: stale ? state.data : null, error, pending: false, stale, canContinue: false }); throw error;
      } finally {
        if (rejectAbort) current.signal.removeEventListener("abort", rejectAbort);
        if (id === sequence) controller = null;
      }
    },
    clear,
    dispose() { if (!disposed) { clear(); disposed = true; stop(); listeners.clear(); } }
  };
}
