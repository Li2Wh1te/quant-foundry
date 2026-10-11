import { readApiToken } from "../../../auth/tokenStorage";
import { dataAssetsClient, type DataAssetsClient } from "./client";
import { apiError, cancellation, clearsPreview, invalidRequest } from "./errors";
import { onDataAssetsInvalidation } from "./lifecycle";
import type { PreviewRequest, PreviewResult, PreviewSessionSnapshot, RequestOptions } from "./types";
import { validatePreviewRequest } from "./validation";

export interface RequestScope {
  run<T>(operation: (signal: AbortSignal) => Promise<T>, options?: Pick<RequestOptions, "signal">): Promise<T>;
  cancel(): void;
  dispose(): void;
}

/** One page-owned latest-request slot. Dispose it on unmount or logout. */
export function createRequestScope(): RequestScope {
  let active: AbortController | null = null, sequence = 0, disposed = false;
  const cancel = () => { sequence++; active?.abort(cancellation()); active = null; };
  return {
    cancel,
    dispose() { disposed = true; cancel(); },
    async run(operation, options = {}) {
      cancel();
      if (disposed || options.signal?.aborted) throw cancellation();
      const controller = new AbortController(), version = sequence, token = readApiToken();
      active = controller;
      const abort = () => controller.abort(cancellation());
      options.signal?.addEventListener("abort", abort, { once: true });
      let rejectAbort: (() => void) | undefined;
      const cancelled = new Promise<never>((_resolve, reject) => {
        rejectAbort = () => reject(cancellation());
        controller.signal.addEventListener("abort", rejectAbort, { once: true });
      });
      // Explicit logout also cancels page-owned operations outside the HTTP
      // transport. Auth failures stay with their originating transport so a
      // caller still receives its 401/403 rather than a masking AbortError.
      const stopListening = onDataAssetsInvalidation(reason => {
        if (reason === "session") abort();
      });
      try {
        const result = await Promise.race([Promise.resolve().then(() => {
          if (controller.signal.aborted) throw cancellation();
          return operation(controller.signal);
        }), cancelled]);
        if (disposed || version !== sequence || controller.signal.aborted || token !== readApiToken()) throw cancellation();
        return result;
      } finally {
        stopListening();
        if (active === controller) active = null;
        options.signal?.removeEventListener("abort", abort);
        if (rejectAbort) controller.signal.removeEventListener("abort", rejectAbort);
      }
    }
  };
}

export interface PreviewSession {
  read(request: PreviewRequest, options?: RequestOptions & { page?: number }): Promise<PreviewResult>;
  getSnapshot(): PreviewSessionSnapshot;
  reset(): void;
  dispose(): void;
}

function cloneResult(result: PreviewResult | null): PreviewResult | null {
  return result && { ...result, rows: result.rows.map(row => ({ ...row })), actual_range: { ...result.actual_range },
    selected_partitions: [...result.selected_partitions], limitations: [...result.limitations],
    ...(result.semantics ? { semantics: { ...result.semantics } } : {}) };
}

/**
 * Only the displayed page and obtained cursor stack survive within this session.
 * Scope changes/permission revocation/data changes clear both; ordinary refresh
 * failures retain the previously permitted page and its original observation time.
 */
export function createPreviewSession(client: DataAssetsClient = dataAssetsClient): PreviewSession {
  const scope = createRequestScope();
  let result: PreviewResult | null = null, cursors: (string | null)[] = [null], page = 0;
  let readAt: string | null = null, requestKey: string | null = null, token = readApiToken(), revision = 0;
  const clear = () => { result = null; cursors = [null]; page = 0; readAt = null; requestKey = null; revision++; };
  const reset = () => { scope.cancel(); clear(); token = readApiToken(); };
  const stopListening = onDataAssetsInvalidation(reason => {
    // Preserve the originating 401/403 error for the existing auth handler. The
    // transport aborts other requests; a revision also rejects injected late results.
    clear();
    if (reason === "session") scope.cancel();
  });
  return {
    reset,
    dispose() { scope.dispose(); clear(); stopListening(); },
    getSnapshot() {
      if (token !== readApiToken()) reset();
      return { result: cloneResult(result), page, cursors: [...cursors], readAt };
    },
    async read(request, options = {}) {
      if (token !== readApiToken()) reset();
      const body = validatePreviewRequest(request);
      const key = JSON.stringify({ ...body, cursor: null });
      const target = options.page ?? 0;
      if (!Number.isSafeInteger(target) || target < 0) invalidRequest("page");
      if (requestKey !== key) { reset(); requestKey = key; }
      if (target >= cursors.length || (target > 0 && !cursors[target])) invalidRequest("page");
      if (body.cursor !== null && body.cursor !== cursors[target]) invalidRequest("query.cursor");
      const version = revision;
      try {
        const next = await scope.run(signal => client.queryPreview({ ...body, cursor: cursors[target] },
          { ...options, signal }), options);
        if (version !== revision || token !== readApiToken()) throw cancellation();
        if (target > 0 && (result?.generation == null || next.generation == null)) {
          reset(); throw apiError(502, "INVALID_RESPONSE");
        }
        if (target > 0 && next.generation !== result?.generation) throw apiError(409, "DATA_CHANGED");
        if (["restricted", "rebuilding", "rebuild_required"].includes(next.status)) {
          throw apiError(409, next.status === "restricted" ? "DATA_RESTRICTED"
            : next.status === "rebuilding" ? "DATA_STORE_REBUILDING" : "REBUILD_REQUIRED");
        }
        if (next.next_cursor && cursors.slice(1, target + 1).includes(next.next_cursor)) throw apiError(409, "INVALID_CURSOR");
        result = cloneResult(next); page = target; readAt = new Date().toISOString();
        cursors = target === 0 ? [null] : cursors.slice(0, target + 1);
        if (next.next_cursor) cursors.push(next.next_cursor);
        return cloneResult(next)!;
      } catch (error) {
        // A superseded request must not erase the new object's page/session.
        if (version === revision && clearsPreview(error)) reset();
        throw error;
      }
    }
  };
}
